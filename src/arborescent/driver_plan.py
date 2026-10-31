"""The driver's use case (#67): translate one package's migration arguments into a library request, and supply it.

No migration happens here and no tool's command line is known. The only decisions are whether the annotations allow a
hand-over at all (an open error gap blocks it) and whether the package leaves this repository (a workspace member does not).
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import PolicyError
from .driver_port import MigrationLibrary
from .driver_request import (
    ImportRewrite,
    MigrationOutcome,
    MigrationRequest,
    MigrationSite,
)
from .migration_arguments import (
    ERROR,
    Bypass,
    Gap,
    MigrationArguments,
    PackageArguments,
)
from .package_model import ModuleRename

__all__ = ["BLOCKED", "READY", "STAYS", "DriverPlan", "plan_supply", "supply"]

READY = "ready"  # the library can be supplied
BLOCKED = "blocked"  # an error gap is open, or other packages still import the package's private modules
STAYS = "stays"  # a workspace member: no new repository, so no history to extract


@dataclass(frozen=True, slots=True)
class DriverPlan:
    package: str
    status: str
    reason: str
    gaps: tuple[Gap, ...]
    bypasses: tuple[Bypass, ...]  # modules of other packages importing it outside its public surface
    request: MigrationRequest | None
    arguments: tuple[str, ...]  # what the library would be called with; empty unless ready
    unapplied: tuple[str, ...]  # arguments the library cannot apply
    rewrite: ImportRewrite | None = None  # the import-rewriting step; None when no module changes its name


def plan_supply(arguments: MigrationArguments, package_id: str, library: MigrationLibrary, site: MigrationSite) -> DriverPlan:
    package = next((item for item in arguments.packages if item.id == package_id), None)
    if package is None:
        known = ", ".join(item.id for item in arguments.packages) or "none"
        raise PolicyError(f"unknown package {package_id!r}; the catalog has: {known}")
    gaps = tuple(gap for gap in arguments.gaps if gap.severity == ERROR and gap.package in (None, package_id))
    bypasses = package.bypasses
    if gaps or bypasses:
        # No override: with either open, the packages left behind would import something that is no longer there.
        why = [f"{len(gaps)} open annotation gap(s) of severity error"] if gaps else []
        if bypasses:
            why.append(f"{len(bypasses)} import(s) of its private modules by other packages (a split would break them)")
        return DriverPlan(package_id, BLOCKED, "; ".join(why), gaps, bypasses, None, (), ())
    home = package.home
    if home is not None and home.kind == "workspace-member":
        return DriverPlan(
            package_id, STAYS, f"workspace member at {home.path}: it stays in this repository, so there is no history to extract",
            (), (), None, (), (),
        )
    request = MigrationRequest(package_id, site.source, site.target, site.branch, package.path_renames, package.module_renames)
    return DriverPlan(package_id, READY, "ready", (), (), request, library.arguments(request), library.unsupported(request), _rewrite_of(package))


def _rewrite_of(package: PackageArguments) -> ImportRewrite | None:
    """The per-module map, longest old name first so a package's own rename never runs before its children's."""
    renames = tuple(sorted(
        (ModuleRename(item.module, item.target_module) for item in package.modules if item.module != item.target_module),
        key=lambda rename: (-len(rename.source), rename.source),
    ))
    if not renames:
        return None
    return ImportRewrite(renames, tuple(r.source for r in package.path_renames), tuple(r.target for r in package.path_renames))


def supply(plan: DriverPlan, library: MigrationLibrary) -> MigrationOutcome:
    """Call the library with the planned request; refuse (without calling it) when the plan is not ready."""
    if plan.request is None:
        return MigrationOutcome(False, f"{plan.package}: {plan.reason}")
    if not library.available():
        return MigrationOutcome(False, f"{library.executable} is not installed")
    return library.supply(plan.request)
