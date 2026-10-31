"""Gather the repository facts for the package-assignment rules (#52) and run them. The rules are in ``package_assignment``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .domain import Policy, Violation
from .package_assignment import RepositoryFacts, assignment_violations
from .package_contracts import PackageLayout, layout_of
from .package_model import CatalogDocument, CatalogError
from .package_parts import load_parts, module_externals, module_imports, public_symbols
from .package_preservation import PartFacts, preservation_violations
from .package_tree import is_generated
from .packages import read_catalog
from .path_exclusion import excluded_by_policy
from .ports import FileSystem, ModuleScanner, ScanResult
from .rules import RuleContext

__all__ = [
    "PackageContext", "asset_paths", "load_package_catalog", "load_package_context", "package_gap_violations", "package_layout",
    "repository_facts",
]


def load_package_catalog(fs: FileSystem, root: Path, policy: Policy) -> CatalogDocument:
    """The configured catalog, parsed only: gaps (including double claims that validation refuses) are findings here."""
    return read_catalog(fs, fs.resolve(root), policy.package_catalog or "packages.toml")


@dataclass(frozen=True, slots=True)
class PackageContext:
    """The catalog and the repository facts it is checked against (also what the selftest plans its probes from)."""

    catalog: CatalogDocument
    facts: RepositoryFacts
    parts: tuple[PartFacts, ...] = ()


def load_package_context(
    policy: Policy, root: Path, fs: FileSystem, scan: ScanResult, scanner: ModuleScanner | None = None
) -> PackageContext | None:
    """``None`` when the policy does not set ``package_catalog``. Configured parts are scanned with ``scanner``."""
    if policy.package_catalog is None:
        return None
    facts = repository_facts(policy, root, fs, scan)
    if not policy.package_parts:
        return PackageContext(load_package_catalog(fs, root, policy), facts)
    if scanner is None:
        raise CatalogError("`package_parts` needs a module scanner to read the parts")
    return PackageContext(load_package_catalog(fs, root, policy), facts, load_parts(policy, root, fs, scanner, scan.known))


def package_layout(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> PackageLayout | None:
    """What ``generate`` compiles into the engine configs; ``None`` when the policy does not set ``package_catalog``."""
    if policy.package_catalog is None:
        return None
    return layout_of(load_package_catalog(fs, root, policy), scan.paths)


def package_gap_violations(
    policy: Policy, root: Path, fs: FileSystem, scan: ScanResult, scanner: ModuleScanner | None = None
) -> list[Violation]:
    """Findings for the configured catalog (assignment, #52; preservation across parts, #55); none without ``package_catalog``."""
    context = load_package_context(policy, root, fs, scan, scanner)
    if context is None:
        return []
    return assignment_violations(context.catalog, context.facts) + preservation_violations(
        context.catalog, context.facts, context.parts
    )


def repository_facts(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> RepositoryFacts:
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    return RepositoryFacts(
        dict(scan.paths),
        {name: context.resolve_ring(name) for name in scan.paths},
        module_imports(scan.modules, scan.known),
        asset_paths(policy, root, fs),
        symbols=public_symbols(scan.modules),
        externals=module_externals(scan.modules, scan.known, policy.stdlib),
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
