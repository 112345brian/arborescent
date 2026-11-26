"""Shared vocabulary `policy_rings`/`policy_imports`, `architecture_policy` and `domain` all need, with no dependency back
on any of them, including `ArchitectureFields`, the per-scope rule data the query facets read. `domain` re-exports these
names so every existing `from .domain import PolicyError` (etc.) caller is unaffected.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .presets import DEFAULT_PRESET, IO_MODULES, PRESETS, Preset

__all__ = [
    "ALL", "ArchitectureFields", "ConstructionSite", "DocsPolicy", "NameCollisionPolicy", "OwnedLiteral", "PolicyError", "PortBinding", "ResultDiscard", "RingRule",
    "in_unit", "normalized_source_path", "path_under", "unit_permits",
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


@dataclass(frozen=True)
class NameCollisionPolicy:
    """Opt-in ``[name_collisions]`` (#221): note a field or local that reuses the name of an unrelated type."""

    min_length: int = 5  # shorter names are too generic to mean one thing
    words: tuple[str, ...] = ()  # words that already mean one concept: they match a type by its last word too
    allow: tuple[tuple[str, str], ...] = ()  # (name, reason): never noted


_DOUBLE_MODULES = frozenset({"tests", "testing", "conftest", "fakes"})
_DOUBLE_CLASS = re.compile(r"_*(Fake|Stub|Mock)(?![a-z])")


@dataclass(frozen=True)
class PortBinding:
    """A port that must have a production implementor, and no more than ``max_implementors`` of them (#219)."""

    port: str  # dotted name of the Protocol or abstract class, e.g. "app.ports.Clock"
    max_implementors: int = 1
    exclude: tuple[str, ...] = ()  # module globs or `module.Class` names that are not production bindings
    reason: str = ""

    @property
    def port_module(self) -> str:
        return self.port.rpartition(".")[0]

    def counts(self, module: str, class_name: str) -> bool:
        """Whether a class is production code. Test doubles are not: a ``tests``/``testing``/``conftest``/``fakes`` or
        ``test_*`` module, a ``Fake*``/``Stub*``/``Mock*`` class, and whatever ``exclude`` names."""
        if any(part in _DOUBLE_MODULES or part.startswith("test_") for part in module.split(".")):
            return False
        if _DOUBLE_CLASS.match(class_name.rpartition(".")[2]):
            return False
        full = f"{module}.{class_name}"
        return not any(in_unit(module, glob) or in_unit(full, glob) for glob in self.exclude)


@dataclass(frozen=True)
class ResultDiscard:
    """Opt-in ``result_discard`` (#141): a bare call statement that drops the value of a result-bearing function."""

    functions: tuple[str, ...] = ()  # dotted names marked result-bearing in addition to the naming convention
    rings: tuple[str, ...] = ()  # rings whose modules are checked; empty means every ring
    convention: bool = True  # a module-level `-> *Result` / `-> *Outcome` function is result-bearing


@dataclass(frozen=True)
class DocsPolicy:
    """``[tool.arborescent.docs]``: which module documentation rules are on (#110)."""

    module_docstring: bool = False  # every module has a non-blank docstring
    module_names_ring: bool = False  # the docstring says the module's effective ring, as a whole word
    exclude: tuple[str, ...] = ()  # module globs the rules skip

    def applies_to(self, module: str) -> bool:
        return not any(in_unit(module, glob) for glob in self.exclude)


@dataclass(frozen=True)
class ArchitectureFields:
    """The data half of `architecture_policy.ArchitecturePolicy`: what one module or ring obeys, settable per scope.

    The fields are `config._LEGACY_ARCHITECTURE_KEYS` as parsed (`ring`, `groups` and `middle_forbid` fold into `rings` and
    `preset`; `git_hook` is read off the raw table), plus `disabled_tools`: a root global every scope inherits unchanged,
    kept here because the annotation queries consult it. Pure data, so `policy_rings`/`policy_imports` can type against
    it without importing the class whose methods delegate to them (that import would be a cycle)."""

    rings: tuple[RingRule, ...]  # innermost first
    classify: tuple[tuple[str, str], ...]  # (glob on dotted module name, ring); first match wins
    sources: tuple[str, ...] = (".",)
    source_roots: tuple[str, ...] = (".",)
    module_forbid_imports: tuple[tuple[str, tuple[str, ...]], ...] = ()
    owned_literals: tuple[OwnedLiteral, ...] = ()
    construct_only: tuple[ConstructionSite, ...] = ()  # #85
    result_discard: ResultDiscard | None = None
    port_bindings: tuple[PortBinding, ...] = ()  # #219
    docs: DocsPolicy = DocsPolicy()
    importable_by: tuple[tuple[str, tuple[str, ...]], ...] = ()  # module glob -> globs of the only modules that may import it
    entry_scripts: tuple[str, ...] = ()  # dotted module names that are PEP 723 scripts
    dependencies: Mapping[str, str] = field(default_factory=lambda: {})  # import name -> distribution
    forbid_everywhere: tuple[str, ...] = ()
    forbidden_modules: tuple[str, ...] = ()  # globs on dotted first-party module names that must not exist
    effect_defaults: str = "injection"  # how a parameter default naming an effect counts: injection | typed | count
    tach_external_exclude: tuple[str, ...] = ()  # written to tach `[external] exclude`
    forbid_circular_imports: bool = True
    preset: Preset = PRESETS[DEFAULT_PRESET]
    io_modules: frozenset[str] = IO_MODULES  # stdlib modules that mean "I/O, process or OS"
    require_arbor_annotations: bool = False
    require_arbor_annotations_exclude: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()  # root global inherited by every scope; `arbor-annotations` switches annotations off
