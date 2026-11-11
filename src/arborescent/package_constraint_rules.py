"""Enforcement of ``[package.constraints]`` (#165): what a package promises *not* to do, checked on facts and on placement.

Two rules, both per module and judged against that module's own scope:

* ``package-constraint-facts``: the module uses a forbidden effect, or (``must_be_pure``) imports something that is not first-party or
  pure standard library, or imports a first-party module that reaches a forbidden effect. Whatever ring the module is in.
* ``package-constraint-placement``: the module's ring permits an effect the constraint forbids, so loosening a ring fails before any
  module misbehaves.

The effect facts are the scanner's own per-function effect analysis and its category taxonomy; nothing here detects an effect.
Limits: reachability is the file-level first-party import graph of the same scope (type-only imports never run, so they carry nothing);
a third-party package's own behaviour is not looked into, only the import is refused; ``must_be_pure`` placement judges effects, not the
ring's ``external`` list. Test, docs and developer-tooling paths are exempt. Pure.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import Policy, Rule, Violation
from .facts import ModuleInfo, resolve_targets
from .package_constraints import PackageConstraints
from .presets import EFFECT_CATEGORIES, STRUCTURAL_STDLIB

__all__ = ["PURE_STDLIB", "forbidden_by", "is_exempt_path", "placement_violations", "reach_violations", "use_violations"]

# Standard-library modules that compute and never touch the clock, the OS, the network or a stream: what ``must_be_pure`` may import.
PURE_STDLIB: frozenset[str] = STRUCTURAL_STDLIB | frozenset({
    "math", "cmath", "re", "functools", "itertools", "operator", "string", "textwrap", "fractions", "decimal", "numbers", "statistics",
    "heapq", "bisect", "copy", "types", "keyword", "difflib", "fnmatch", "json", "base64", "binascii", "hashlib", "hmac", "unicodedata",
    "ipaddress", "graphlib", "pprint", "reprlib", "contextlib", "array", "colorsys", "shlex",
})  # fmt: skip

_PURE_EXCLUDES = frozenset({"patch"})  # a test-only seam: no source module uses it, so purity never has to name it
_EXEMPT_DIRECTORIES = frozenset({"tests", "test", "docs"})


def forbidden_by(constraints: PackageConstraints) -> frozenset[str]:
    """The effect categories the constraints rule out (every one, bar the test-only ``patch``, for ``must_be_pure``)."""
    named = frozenset(constraints.forbid_effects)
    return named | (EFFECT_CATEGORIES - _PURE_EXCLUDES) if constraints.must_be_pure else named


def is_exempt_path(path: str) -> bool:
    """Constraints apply to source: a path below a ``tests``, ``test`` or ``docs`` folder, or a ``conftest.py``, is exempt."""
    parts = path.replace("\\", "/").split("/")
    return bool(_EXEMPT_DIRECTORIES.intersection(parts[:-1])) or parts[-1] == "conftest.py"


def _source(constraints: PackageConstraints) -> str:
    return "must_be_pure" if constraints.must_be_pure else "forbid_effects"


def use_violations(
    constraints: PackageConstraints, policy: Policy, info: ModuleInfo, ring: str | None, imports: tuple[Collection[str], Collection[str]],
) -> list[Violation]:
    """``package-constraint-facts`` for one module's own effect uses and, under ``must_be_pure``, its external imports.

    ``imports`` is ``(known modules, their top-level names)``."""
    if is_exempt_path(info.path):
        return []
    forbidden = forbidden_by(constraints)
    where = f"[{ring}] " if ring is not None else ""
    out = [
        Violation(Rule.PACKAGE_CONSTRAINT_FACTS, info.name, f"{where}effect {use.category} uses {use.target}; {_source(constraints)} forbids it", use.line)
        for use in info.facts.effect_uses
        if use.category in forbidden and policy.counts_effect_seam(use.seam)
    ]
    if constraints.must_be_pure:
        known, tops = imports
        for imp in info.facts.imports:
            if imp.type_only:
                continue
            _, external, _ = resolve_targets(info.name, info.is_package, imp, known, tops)
            out.extend(
                Violation(Rule.PACKAGE_CONSTRAINT_FACTS, info.name, f"{where}imports {top}; must_be_pure allows only first-party and pure standard-library imports", imp.line)
                for top in dict.fromkeys(external)
                if top not in PURE_STDLIB
            )
    return out


def reach_violations(
    constraints: PackageConstraints, name: str, line: int, targets: Collection[str], reachable: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> list[Violation]:
    """``must_be_pure`` only: an import of a first-party module from which a forbidden effect is reachable (file-level)."""
    if not constraints.must_be_pure:
        return []
    forbidden = forbidden_by(constraints)
    out: list[Violation] = []
    for target in targets:
        out.extend(
            Violation(
                Rule.PACKAGE_CONSTRAINT_FACTS, name,
                f"imports {target!r}; effect {category} reaches it via {' -> '.join(chain)}; must_be_pure forbids it", line,
            )
            for category, chain in reachable.get(target, {}).items()
            if category in forbidden
        )
    return out


def placement_violations(constraints: PackageConstraints, policy: Policy, info: ModuleInfo, ring: str) -> list[Violation]:
    """``package-constraint-placement``: the ring this module is placed in permits an effect the package promises not to use."""
    if is_exempt_path(info.path):
        return []
    permitted = sorted(forbidden_by(constraints) - policy.forbidden_effects(ring))
    if not permitted:
        return []
    detail = f"[{ring}] ring permits {', '.join(permitted)}, which {_source(constraints)} forbids; tighten the ring's `forbid` or drop the constraint"
    return [Violation(Rule.PACKAGE_CONSTRAINT_PLACEMENT, info.name, detail)]
