"""Selftest mutations for the package-assignment rules (#52).

Each probe edits a copy of the repository so one rule must newly fire: a new file nobody owns, or text appended to the
catalog (a second package claiming an owned file, a glob contradicting a path owner) or to a module (a new import).
A probe that cannot be built for this catalog says why, instead of silently disappearing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import PurePosixPath

from .domain import Policy, Rule
from .mutation import Mutation
from .package_assignment import owners_of
from .package_fields import glob_match
from .package_gaps import PackageContext
from .rules import RuleContext

__all__ = ["package_mutations"]

_PROBE_ID = "arborescent-probe"


def package_mutations(
    policy: Policy, arbor_index: Mapping[str, tuple[str | None, str | None]], scope_by_module: Mapping[str, str],
    context: PackageContext,
) -> tuple[list[Mutation], list[str]]:
    catalog, facts = context.catalog, context.facts
    catalog_file = policy.package_catalog or "packages.toml"
    sole = {module: owners[0] for module, path in sorted(facts.modules.items()) if len(owners := owners_of(catalog, path)) == 1}
    out: list[Mutation] = []
    skipped: list[str] = []
    folders = sorted({PurePosixPath(path).parent.as_posix() for path in facts.modules.values()})
    for rule, name, asset in (
        (Rule.PACKAGE_UNASSIGNED_MODULE, "arborescent_probe_unassigned.py", False),
        (Rule.PACKAGE_UNOWNED_ASSET, "arborescent_probe_unowned.arborescent-probe", True),
    ):
        path = next((p for folder in folders if not owners_of(catalog, p := _join(folder, name), asset=asset)), None)
        if path is None:
            skipped.append(f"{rule}: every source folder is wholly claimed by a package, so no new file can be unowned")
        else:
            out.append(Mutation(rule, f"a new {'asset' if asset else 'module'} no package claims", path, "", new_file=True))
    out += _second_claims(catalog_file, context, sole, skipped)
    out += _ring_span(RuleContext(policy, arbor_index, scope_by_module).resolve_ring, catalog_file, context, sole, skipped)
    out += _undeclared_import(context, sole, skipped)
    out += _pyproject_drift(context, skipped)
    return out, skipped


def _join(folder: str, name: str) -> str:
    return name if folder in ("", ".") else f"{folder}/{name}"


def _probe_package(*paths: str) -> str:
    owned = "".join(f'[[packages.paths]]\npath = "{path}"\nrole = "source"\n' for path in paths)
    return (
        f'[[packages]]\nid = "{_PROBE_ID}"\nname = "Probe"\nkind = "library"\nrepository = "probe"\n'
        f'root = "."\nsummary = "selftest probe"\n{owned}'
    )


def _second_claims(
    catalog_file: str, context: PackageContext, sole: dict[str, str], skipped: list[str]
) -> list[Mutation]:
    catalog, facts = context.catalog, context.facts
    out: list[Mutation] = []
    module = next(iter(sorted(sole)), None)
    if module is None:
        skipped.append(f"{Rule.PACKAGE_MULTIPLY_ASSIGNED_MODULE}: no module is claimed by exactly one package")
    else:
        out.append(Mutation(
            Rule.PACKAGE_MULTIPLY_ASSIGNED_MODULE, f"a second package claiming {module}",
            catalog_file, _probe_package(facts.modules[module]),
        ))
    asset = next((
        path for path in sorted(facts.assets)
        if len(owners_of(catalog, path)) == 1 and not any(glob_match(rule.pattern, path) for rule in catalog.assets)
    ), None)
    if asset is None:
        skipped.append(f"{Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET}: no asset is claimed by exactly one package path and no glob")
    else:
        out.append(Mutation(
            Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET, f"a glob giving {asset} to a second package",
            catalog_file, f'{_probe_package()}\n[[assets]]\npattern = "{asset}"\npackage = "{_PROBE_ID}"',
        ))
    return out


def _ring_span(
    ring_of: Callable[[str], str | None], catalog_file: str, context: PackageContext, sole: dict[str, str],
    skipped: list[str],
) -> list[Mutation]:
    by_ring: dict[str, str] = {}
    for module in sorted(sole):
        ring = ring_of(module)
        if ring is not None:
            by_ring.setdefault(ring, module)
    if len(by_ring) < 2:
        skipped.append(f"{Rule.PACKAGE_SPANS_RINGS}: fewer than two rings have a module owned by exactly one package")
        return []
    first, second = list(by_ring.values())[:2]
    return [Mutation(
        Rule.PACKAGE_SPANS_RINGS, f"a package claiming {first} and {second}, which are in different rings",
        catalog_file, _probe_package(context.facts.modules[first], context.facts.modules[second]),
    )]


def _undeclared_import(context: PackageContext, sole: dict[str, str], skipped: list[str]) -> list[Mutation]:
    declared = {package.id: set(package.depends_on) for package in context.catalog.packages}
    for host, owner in sorted(sole.items()):
        target = next((
            module for module, other in sorted(sole.items())
            if other != owner and other not in declared.get(owner, set())
            and module not in context.facts.imports.get(host, ())
        ), None)
        if target is not None:
            return [Mutation(
                Rule.PACKAGE_UNDECLARED_DEPENDENCY, f"{host} importing {target} of a package it does not depend on",
                context.facts.modules[host], f"import {target}",
            )]
    skipped.append(f"{Rule.PACKAGE_UNDECLARED_DEPENDENCY}: no two packages without a declared dependency to import across")
    return []


def _pyproject_drift(context: PackageContext, skipped: list[str]) -> list[Mutation]:
    """A workspace source for a package nobody depends on, appended to an existing derived file."""
    if not context.derived:
        skipped.append(f"{Rule.PACKAGE_PYPROJECT_DRIFT}: package_pyproject is off or no derived pyproject.toml exists to edit")
        return []
    return [Mutation(
        Rule.PACKAGE_PYPROJECT_DRIFT, f"a workspace source for a non-dependency in {context.derived[0]}",
        context.derived[0], f"[tool.uv.sources.{_PROBE_ID}]\nworkspace = true",
    )]
