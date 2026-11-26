"""The ``[effects]`` policy table (0.8.0): which modules are sanctioned capability seams. Also ``[unguarded_module_state]`` (#232).

A seam is the module that owns one effect on purpose (a file-read port, a process-environment reader, the logging setup)
so that everything else can take it as a port. ``effect-via-import`` used to report every importer of such a module,
which is the sanctioned path itself. Naming the seam here stops its *own* direct uses from propagating to importers;
the guard that makes that safe is ``effect-unsanctioned-import``: a module in a ring that forbids effects may import a
module of a provider ring only if the seam is listed. Without ``sanctioned`` the table is off and nothing changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

DEFAULT_PROVIDER_RINGS = ("effects",)


@dataclass(frozen=True)
class EffectsPolicy:
    """``sanctioned``: dotted names, prefixes or globs of seam modules (matched like ``classify`` globs). ``rings``: the rings that hold
    modules which exist to use effects; the guard applies to imports of their modules. ``configured`` is true when the policy sets
    ``sanctioned`` (an empty list still switches the guard on: nothing is sanctioned yet)."""

    sanctioned: tuple[str, ...] = ()
    rings: tuple[str, ...] = DEFAULT_PROVIDER_RINGS
    configured: bool = False

    def __post_init__(self) -> None:
        if not self.rings:
            raise ValueError("`effects.rings` must name at least one ring")


@dataclass(frozen=True)
class StateAllow:
    """A module-level name that may be written without a lock (a single-threaded entrypoint, say), with the reason."""

    module: str  # a dotted module name, package prefix or glob
    name: str  # the module-level name, or a glob
    reason: str


@dataclass(frozen=True)
class ModuleStatePolicy:
    """``[unguarded_module_state]``: the rule is on when the table exists. ``rings``: where a finding fails (empty: every ring);
    elsewhere it is a note. ``locks`` and ``guard_decorators`` are dotted names or globs for what the scan cannot recognise itself."""

    rings: tuple[str, ...] = ()
    locks: tuple[str, ...] = ()  # lock objects or context-manager factories used as `with <name>:` / `with <name>(...):`
    guard_decorators: tuple[str, ...] = ()
    allow: tuple[StateAllow, ...] = ()

    def fails_in(self, ring: str) -> bool:
        return not self.rings or ring in self.rings

    def names_guard(self, held: tuple[str, ...], decorators: tuple[str, ...]) -> bool:
        return any(fnmatchcase(name, pattern) for name in held for pattern in self.locks) or any(
            fnmatchcase(name, pattern) for name in decorators for pattern in self.guard_decorators
        )

    def allowing(self, module: str, name: str) -> tuple[StateAllow, ...]:
        return tuple(
            entry for entry in self.allow
            if (module == entry.module or module.startswith(entry.module + ".") or fnmatchcase(module, entry.module))
            and fnmatchcase(name, entry.name)
        )
