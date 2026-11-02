"""#90: does a module's `[classify]` ring agree with what its code does? Three facts-only signals, information only.

Rule id ``classify-facts``. Nothing here reads names or guesses intent: each signal is a statement about imports, effect uses,
top-level definitions and a ``__main__`` guard, all of which the scan already holds. A note is a prompt to look, never a verdict:
the scanner does not see builtins such as ``open()`` or ``print()``, calls through objects, or what a function does with its arguments.

1. *could sit in an inner ring*: the module passes every per-module rule of a more inner ring.
2. *composition-shaped*: a leaf that defines functions only and imports two or more peers of its ring plus inner-ring code.
3. *program and library*: a module with a top-level ``if __name__ == "__main__"`` that other modules import, outside the
   ring that may import every ring (where a program belongs).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .domain import ALL, Policy, Rule, Violation
from .facts import ModuleInfo, resolve_targets
from .rules import RuleContext, fits_ring

__all__ = ["audit_classification"]

_SHOWN = 3


def _names(modules: Collection[str]) -> str:
    ordered = sorted(modules)
    more = f" and {len(ordered) - _SHOWN} more" if len(ordered) > _SHOWN else ""
    return ", ".join(ordered[:_SHOWN]) + more


def _internal_targets(context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> dict[str, set[str]]:
    tops = {m.split(".")[0] for m in known}
    out: dict[str, set[str]] = {name: set() for name in modules}
    for name, info in modules.items():
        for imp in info.facts.imports:
            internal, _, error = resolve_targets(name, info.is_package, imp, known, tops)
            if error is None:
                out[name] |= {
                    t for t in internal if t != name and t in modules and context.scope_name(t) == context.scope_name(name)
                }
    return out


@dataclass(frozen=True)
class _Subject:
    """One module under judgement, with the policy and ring it is judged against and the import graph around it."""

    context: RuleContext
    policy: Policy
    name: str
    info: ModuleInfo
    ring: str
    targets: Mapping[str, set[str]]  # module -> first-party modules it imports
    importers: Mapping[str, set[str]]  # module -> first-party modules that import it

    @property
    def outermost_may_import_all(self) -> bool:
        return ALL in self.policy.rings[-1].may_import


def _inner_ring(subject: _Subject, known: Collection[str]) -> Violation | None:
    facts = subject.info.facts
    if not facts.top_level_defs or facts.main_guard or any(d.is_abstract for d in facts.top_level_defs):
        return None
    if subject.name in subject.policy.entry_scripts:
        return None
    names = [r.name for r in subject.policy.rings]
    for candidate in names[: names.index(subject.ring)]:
        if fits_ring(subject.context, subject.name, subject.info, candidate, known):
            detail = (
                f"[{subject.ring}] could sit in ring {candidate!r}: its imports, external packages and effects all pass that ring's rules"
            )
            return Violation(Rule.CLASSIFY_FACTS, subject.name, detail)
    return None


def _composition_shaped(subject: _Subject) -> Violation | None:
    outermost = subject.policy.rings[-1]
    defs = subject.info.facts.top_level_defs
    if subject.ring == outermost.name or not subject.outermost_may_import_all or subject.info.is_package:
        return None
    if subject.importers[subject.name] or not defs or any(d.is_class for d in defs):
        return None
    rings = {t: subject.context.resolve_ring(t) for t in subject.targets[subject.name]}
    peers = {t for t, ring in rings.items() if ring == subject.ring}
    inner = {t for t, ring in rings.items() if ring not in (None, subject.ring)}
    if len(peers) < 2 or not inner:
        return None
    detail = (
        f"[{subject.ring}] defines no class, nothing imports it, and it imports {len(peers)} peers ({_names(peers)}) and inner-ring "
        f"code ({_names(inner)}): that is the shape of ring {outermost.name!r}, which may import every ring"
    )
    return Violation(Rule.CLASSIFY_FACTS, subject.name, detail)


def _program_and_library(subject: _Subject) -> Violation | None:
    guard = subject.info.facts.main_guard
    importers = subject.importers[subject.name]
    if not guard or not importers:
        return None
    if subject.ring == subject.policy.rings[-1].name and subject.outermost_may_import_all:
        return None  # a program belongs in the ring that may import every ring
    detail = (
        f"[{subject.ring}] runs as a program (`if __name__ == \"__main__\"`, line {guard}) and is imported by "
        f"{len(importers)} module(s) ({_names(importers)}): its command-line code and its importable code share one ring"
    )
    return Violation(Rule.CLASSIFY_FACTS, subject.name, detail, guard)


def audit_classification(context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> list[Violation]:
    targets = _internal_targets(context, modules, known)
    importers: dict[str, set[str]] = {name: set() for name in modules}
    for source, found in targets.items():
        for target in found:
            importers[target].add(source)
    out: list[Violation] = []
    for name, info in sorted(modules.items()):
        policy = context.policy_for(name)
        ring = context.resolve_ring(name)
        if policy is None or ring is None:
            continue
        subject = _Subject(context, policy, name, info, ring, targets, importers)
        found = (_inner_ring(subject, known), _composition_shaped(subject), _program_and_library(subject))
        out += [v for v in found if v is not None]
    return out
