"""Gather the facts of each extracted part (#55): its manifest (#27) and a scan under the policy it ships."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path, PurePosixPath

from .domain import Policy
from .facts import ModuleInfo, resolve_targets
from .package_manifest import load_extracted_package
from .package_model import CatalogError
from .package_preservation import PartFacts
from .package_tree import shipped_policy
from .ports import FileSystem, ModuleScanner
from .rules import RuleContext

__all__ = ["load_parts", "module_imports", "public_symbols"]


def module_imports(modules: Mapping[str, ModuleInfo], known: Collection[str]) -> dict[str, tuple[str, ...]]:
    """Module -> the first-party modules (from ``known``) it imports, other than itself."""
    tops = {name.split(".")[0] for name in known}
    out: dict[str, tuple[str, ...]] = {}
    for name, info in modules.items():
        targets = {
            target
            for imp in info.facts.imports
            for target in resolve_targets(name, info.is_package, imp, known, tops)[0]
            if target in known and target != name
        }
        out[name] = tuple(sorted(targets))
    return out


def public_symbols(modules: Mapping[str, ModuleInfo]) -> dict[str, frozenset[str]]:
    """Module -> its top-level classes and functions whose names do not start with an underscore."""
    return {
        name: frozenset(item.name for item in info.facts.top_level_defs if not item.name.startswith("_"))
        for name, info in modules.items()
    }


def load_parts(
    policy: Policy, root: Path, fs: FileSystem, scanner: ModuleScanner, origin_known: Collection[str]
) -> tuple[PartFacts, ...]:
    """One ``PartFacts`` per configured part; a part that is not a valid extraction or ships no policy is an error."""
    isolated = scanner.uncached()  # the persistent cache is keyed by relative path: a part would evict the origin's entries
    return tuple(_load_part(location, root, fs, isolated, origin_known) for location in policy.package_parts)


def _load_part(location: str, root: Path, fs: FileSystem, scanner: ModuleScanner, origin_known: Collection[str]) -> PartFacts:
    base = fs.resolve(root).joinpath(*PurePosixPath(location).parts)
    if not fs.is_dir(base):
        raise CatalogError(f"package part {location!r} is not a directory in the repository")
    extracted = load_extracted_package(fs, base)
    part_policy = next(
        (found for name in ("arborescent.toml", "pyproject.toml") if (found := shipped_policy(fs, base, base / name)) is not None),
        None,
    )
    if part_policy is None:
        raise CatalogError(
            f"package part {location!r} ships no Arborescent policy (arborescent.toml or [tool.arborescent]); "
            "a part must be able to certify standing alone"
        )
    scan = scanner.scan(part_policy, base)
    context = RuleContext(part_policy, scan.arbor_index, scan.scope_by_module)
    return PartFacts(
        location,
        extracted.included_packages,
        (extracted.semantics, *(item.semantics for item in extracted.dependencies)),
        extracted.paths,
        dict(scan.paths),
        {name: context.resolve_ring(name) for name in scan.paths},
        tuple(rule.name for rule in part_policy.rings),
        public_symbols(scan.modules),
        module_imports(scan.modules, {*scan.known, *origin_known}),
    )
