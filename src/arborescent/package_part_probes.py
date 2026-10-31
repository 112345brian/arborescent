"""Selftest mutations for the annotation-preservation rules (#55).

Each probe edits a copy of the repository so one rule must newly fire. The origin side is edited where a part would
have to lose something (a new public symbol, a new import); the part side where the part would have to change (an
annotation, an extra module, one more manifest path). A probe that cannot be built says why instead of vanishing.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

from .domain import Rule
from .mutation import Mutation
from .package_gaps import PackageContext
from .package_preservation import PartFacts, is_public, public_patterns, sole_owners

__all__ = ["part_mutations"]

_MANIFEST = ".arborescent-package.toml"


def part_mutations(context: PackageContext) -> tuple[list[Mutation], list[str]]:
    """Nothing is planned without configured parts, except the private-import probe, which needs only the catalog."""
    sole = sole_owners(context.catalog, context.facts)
    public = public_patterns(context.catalog)
    out: list[Mutation] = []
    skipped: list[str] = []
    out += _private_import(context, sole, public, skipped)
    if not context.parts:
        return out, skipped
    part = context.parts[0]
    out += _semantics(part, skipped)
    out += _membership(part, skipped)
    out += _ring(context, part, skipped)
    out += _symbol(context, part, sole, public, skipped)
    return out, skipped


def _semantics(part: PartFacts, skipped: list[str]) -> list[Mutation]:
    anchor = next((item for item in part.paths if item.package_id in part.included), None)
    if anchor is None:
        skipped.append(f"{Rule.PACKAGE_SEMANTICS_DRIFT}: the part declares no path to repeat")
        return []
    entry = (
        f'[[paths]]\npackage_id = "{anchor.package_id}"\nrole = "selftest-probe"\n'
        f'origin_path = "arborescent-probe/unowned"\ncurrent_path = "{anchor.current_path}"\n'
    )
    return [Mutation(Rule.PACKAGE_SEMANTICS_DRIFT, "a manifest path the catalog does not declare", _inside(part, _MANIFEST), entry)]


def _membership(part: PartFacts, skipped: list[str]) -> list[Mutation]:
    first = next(iter(sorted(part.module_paths.values())), None)
    if first is None:
        skipped.append(f"{Rule.PACKAGE_MEMBERSHIP_DRIFT}: the part has no module to put a new one next to")
        return []
    folder = PurePosixPath(first).parent.as_posix()
    name = "arborescent_probe_extra.py" if folder in ("", ".") else f"{folder}/arborescent_probe_extra.py"
    return [Mutation(Rule.PACKAGE_MEMBERSHIP_DRIFT, "a module in the part the catalog does not give it", _inside(part, name), "", new_file=True)]


def _ring(context: PackageContext, part: PartFacts, skipped: list[str]) -> list[Mutation]:
    for module in sorted(part.module_paths):
        ring = part.rings.get(module)
        other = next((name for name in part.ring_names if name != ring), None)
        if ring is not None and other is not None and context.facts.rings.get(module) == ring:
            return [Mutation(
                Rule.PACKAGE_RING_DRIFT, f"{module} annotated into ring {other!r} in the part",
                _inside(part, part.module_paths[module]), f'__arbor__ = {{"ring": "{other}"}}',
            )]
    skipped.append(f"{Rule.PACKAGE_RING_DRIFT}: no part module has a ring and the part policy a second ring to move it to")
    return []


def _symbol(
    context: PackageContext, part: PartFacts, sole: Mapping[str, str], public: Mapping[str, tuple[str, ...]], skipped: list[str]
) -> list[Mutation]:
    for module in sorted(part.module_paths):
        owner = sole.get(module)
        if owner is not None and owner in public and module in context.facts.modules and is_public(module, public[owner]) and owner in part.included:
            return [Mutation(
                Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED, f"a new public symbol in {module} that the part lacks",
                context.facts.modules[module], "def arborescent_probe_symbol() -> None:\n    return None",
            )]
    skipped.append(f"{Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED}: no module of an included package matches a declared public surface")
    return []


def _private_import(
    context: PackageContext, sole: Mapping[str, str], public: Mapping[str, tuple[str, ...]], skipped: list[str]
) -> list[Mutation]:
    for host, owner in sorted(sole.items()):
        target = next((
            module for module, other in sorted(sole.items())
            if other != owner and other in public and not is_public(module, public[other])
            and module not in context.facts.imports.get(host, ())
        ), None)
        if target is not None:
            return [Mutation(
                Rule.PACKAGE_PRIVATE_IMPORT, f"{host} importing {target}, outside its package's public surface",
                context.facts.modules[host], f"import {target}",
            )]
    skipped.append(f"{Rule.PACKAGE_PRIVATE_IMPORT}: no package declares a public surface with a module outside it")
    return []


def _inside(part: PartFacts, relative: str) -> str:
    return f"{part.location}/{relative}"
