"""Shared vocabulary `policy_rings`/`policy_imports` and `domain` all need, with no dependency back on either: breaks
what would otherwise be a `domain` <-> `policy_rings`/`policy_imports` circular import. `domain` re-exports these names
so every existing `from .domain import PolicyError` (etc.) caller is unaffected.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from .coupling_policy import CouplingPolicy
from .package_constraints import PackageConstraints
from .presets import Preset
from .purity_policy import PurityPolicy

__all__ = [
    "ALL", "ArchitectureScope", "ConstructionSite", "OwnedLiteral", "PolicyError", "PolicyLike", "RingRule", "in_unit",
    "normalized_source", "path_contains", "path_under", "unit_permits",
]

ALL = "*"  # in `may_import` or `external`: no restriction


class PolicyError(ValueError):
    """The policy is malformed or contradicts itself (reported, never silently repaired)."""


def path_under(rel: str, entry: str) -> bool:
    """Whether repository-relative ``rel`` is ``entry`` or lies below it; both use ``/`` or ``\\`` separators."""
    path = rel.replace("\\", "/").strip("/")
    folder = entry.replace("\\", "/").strip("/")
    return path == folder or path.startswith(folder + "/")


def in_unit(module: str, pattern: str) -> bool:
    """Whether ``module`` is the module ``pattern`` names, one of its submodules, or matches it as a glob."""
    return module == pattern or module.startswith(pattern + ".") or fnmatch.fnmatchcase(module, pattern)


def unit_permits(modules: Sequence[str], rings: Sequence[str], module: str, ring: str | None) -> bool:
    """The one rule behind ``owned_literal`` and ``construct_only``: an empty list allows anyone, else ``module``/``ring`` must match."""
    return (not modules or any(in_unit(module, glob) for glob in modules)) and (not rings or ring in rings)


def normalized_source_path(value: str) -> str:
    return PurePosixPath(value).as_posix().removeprefix("./").rstrip("/") or "."


_NORMALIZED_SOURCES: dict[str, str] = {}


def normalized_source(value: str) -> str:
    """Memoized for configured source roots only (a small fixed set); per-file paths must not enter this cache."""
    cached = _NORMALIZED_SOURCES.get(value)
    if cached is None:
        cached = _NORMALIZED_SOURCES[value] = normalized_source_path(value)
    return cached


def path_contains(source: str, path: str) -> bool:
    source = normalized_source(source)
    return source == "." or path == source or path.startswith(source + "/")


@dataclass(frozen=True)
class RingRule:
    name: str
    may_import: frozenset[str]  # internal rings (its own is implicit); ALL = every inner ring
    external: frozenset[str]  # top-level third-party/stdlib names; ALL = unrestricted
    os_branching: bool = False
    forbid: frozenset[str] | None = None  # None means the positional preset default; [] explicitly allows all effects
    require_abstract: bool = False  # every public class/function in this ring must be an abc.ABC/typing.Protocol shape
    require_typed_ports: bool = False  # every typing.Protocol method in this ring must name its return type (no Any/object/bare container)
    max_reexport_ratio: float | None = None  # share of a module's public surface that may be first-party imports; None = unchecked (#136)


@dataclass(frozen=True)
class OwnedLiteral:
    """A string only some modules and rings may spell (an environment variable, a protocol key): #112."""

    literal: str
    modules: tuple[str, ...] = ()  # module globs; empty means any module
    rings: tuple[str, ...] = ()  # ring names; empty means any ring
    reason: str = ""

    def permits(self, module: str, ring: str | None) -> bool:
        return unit_permits(self.modules, self.rings, module, ring)


@dataclass(frozen=True)
class ConstructionSite:
    """A class or factory only some modules and rings may call (the composition root builds it, nothing else does): #85."""

    target: str  # dotted name of the callable, e.g. "app.queries.UnfilteredQueries"
    modules: tuple[str, ...] = ()  # module globs; empty means any module
    rings: tuple[str, ...] = ()  # ring names; empty means any ring
    reason: str = ""

    @property
    def target_module(self) -> str:
        return self.target.rpartition(".")[0]

    def permits(self, module: str, ring: str | None) -> bool:
        return unit_permits(self.modules, self.rings, module, ring)


class PolicyLike(Protocol):
    """The subset of `Policy`'s fields and methods `policy_rings`/`policy_imports` (and every `scope.policy.*` caller
    elsewhere) actually touch. Exists so `ArchitectureScope.policy` need not name the concrete `Policy` class, which
    would recreate the very `domain` <-> `policy_rings`/`policy_imports` cycle this module was split out to avoid:
    `Policy` structurally satisfies this Protocol, so every real `Policy` instance is already a valid `PolicyLike`."""

    # `@property`, not plain attributes: a Protocol attribute implies read+write, which a frozen dataclass can
    # never satisfy (assignment raises), so `Policy` itself would fail the structural check against this Protocol.
    @property
    def rings(self) -> tuple[RingRule, ...]: ...
    @property
    def classify(self) -> tuple[tuple[str, str], ...]: ...
    @property
    def sources(self) -> tuple[str, ...]: ...
    @property
    def source_roots(self) -> tuple[str, ...]: ...
    @property
    def scopes(self) -> tuple[ArchitectureScope, ...]: ...
    @property
    def disabled_tools(self) -> tuple[str, ...]: ...
    @property
    def require_arbor_annotations(self) -> bool: ...
    @property
    def require_arbor_annotations_exclude(self) -> tuple[str, ...]: ...
    @property
    def effect_defaults(self) -> str: ...
    @property
    def dev_tools_may_import(self) -> tuple[str, ...]: ...
    @property
    def dev_tools(self) -> tuple[str, ...]: ...
    @property
    def forbidden_modules(self) -> tuple[str, ...]: ...
    @property
    def module_forbid_imports(self) -> tuple[tuple[str, tuple[str, ...]], ...]: ...
    @property
    def importable_by(self) -> tuple[tuple[str, tuple[str, ...]], ...]: ...
    @property
    def entry_scripts(self) -> tuple[str, ...]: ...
    @property
    def dependencies(self) -> Mapping[str, str]: ...
    @property
    def forbid_everywhere(self) -> tuple[str, ...]: ...
    @property
    def forbid_circular_imports(self) -> bool: ...
    @property
    def customization_labels(self) -> tuple[str, ...]: ...
    @property
    def preset(self) -> Preset: ...
    @property
    def io_modules(self) -> frozenset[str]: ...
    @property
    def coupling(self) -> CouplingPolicy: ...
    @property
    def package_constraints(self) -> PackageConstraints: ...
    @property
    def mixed_purity(self) -> PurityPolicy: ...
    @property
    def tach_external_exclude(self) -> tuple[str, ...]: ...
    @property
    def owned_literals(self) -> tuple[OwnedLiteral, ...]: ...
    @property
    def construct_only(self) -> tuple[ConstructionSite, ...]: ...

    def scope(self, name: str) -> ArchitectureScope: ...
    def ring(self, name: str) -> RingRule: ...
    def ring_of(self, module: str) -> str | None: ...
    def resolve_ring(self, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None: ...
    def annotation_violation(self, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None: ...
    def annotation_required_violation(self, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None: ...
    def allowed_rings(self, ring: str) -> frozenset[str]: ...
    def counts_effect_seam(self, seam: int) -> bool: ...
    def forbidden_effects(self, ring: str) -> frozenset[str]: ...
    def forbids_module_import(self, module: str, target: str) -> str | None: ...


@dataclass(frozen=True)
class ArchitectureScope:
    """One independently governed source tree and its import boundary."""

    name: str
    policy: PolicyLike
    public: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
