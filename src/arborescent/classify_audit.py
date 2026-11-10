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
from dataclasses import dataclass, replace

from .domain import ALL, Policy, Rule, Violation
from .facts import ModuleInfo, RawImport, resolve_targets
from .rules import RuleContext, fits_ring
from .taint import TaintResult

__all__ = ["AdmissionOutcome", "admission_warnings", "admit_check", "audit_classification", "suggest_ring"]

_SHOWN = 3


def _names(modules: Collection[str]) -> str:
    ordered = sorted(modules)
    more = f" and {len(ordered) - _SHOWN} more" if len(ordered) > _SHOWN else ""
    return ", ".join(ordered[:_SHOWN]) + more


def _taint_detail(taint: TaintResult, name: str) -> str:
    """#98: which of ``name``'s own functions a call-graph taint pass found reaching an I/O primitive, if any."""
    reaching = taint.by_module.get(name, ())
    return "" if not reaching else f"; reaches I/O through a call from {_names(reaching)}"


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
    taint: TaintResult

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
                f"[{subject.ring}] could sit in ring {candidate!r}: its imports, external packages and effects all pass that ring's "
                f"rules{_taint_detail(subject.taint, subject.name)}"
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
        f"{_taint_detail(subject.taint, subject.name)}"
    )
    return Violation(Rule.CLASSIFY_FACTS, subject.name, detail, guard)


def _evidence(info: ModuleInfo, known: Collection[str], name: str) -> str:
    tops = {m.split(".")[0] for m in known}
    internal: set[str] = set()
    external: set[str] = set()
    for imp in info.facts.imports:
        inner, outer, _ = resolve_targets(name, info.is_package, imp, known, tops)
        internal.update(inner)
        external.update(outer)
    effects = sorted({f"{use.category} ({use.target}, line {use.line})" for use in info.facts.effect_uses if use.seam == 0})
    return (
        f"{len(internal)} project import(s), externals {sorted(external) or 'none'}, "
        f"effects {', '.join(effects) if effects else 'none'}"
    )


def suggest_ring(context: RuleContext, name: str, info: ModuleInfo, known: Collection[str]) -> str:
    """#132: the innermost ring whose rules this module already passes, with the facts behind it (``discover --suggest``)."""
    policy = context.policy_for(name)
    if policy is None:
        return "no ring suggestion: the module has no owning architecture scope"
    evidence = _evidence(info, known, name)
    for ring in policy.rings:
        if fits_ring(context, name, info, ring.name, known):
            return f"tightest ring: {ring.name} ({evidence})"
    return f"no ring passes every rule as written ({evidence})"


@dataclass(frozen=True)
class AdmissionOutcome:
    """#161: whether a new module may enter ring ``ring``. A refusal carries every reason; a caller must read ``admitted``."""

    module: str
    ring: str
    admitted: bool
    refusals: tuple[str, ...]
    notes: tuple[str, ...]
    evidence: str


def _strict_info(info: ModuleInfo) -> ModuleInfo:
    """``info`` with each literal-target dynamic import added as an edge, so ``fits_ring`` judges it like any other import."""
    extra = tuple(
        RawImport(0, target, (), line, True, False) for line, target in info.facts.dynamic_imports if target
    )
    return replace(info, facts=replace(info.facts, imports=info.facts.imports + extra))


def _usable_reason(reason: str | None) -> str | None:
    return reason.strip() if reason is not None and reason.strip() and "\x00" not in reason else None


def admit_check(
    context: RuleContext, name: str, info: ModuleInfo, ring: str, known: Collection[str], reason: str | None = None,
) -> AdmissionOutcome:
    """#161: the strict, function-level admission test for ``name`` entering ``ring``.

    Every import counts (function-local, ``TYPE_CHECKING``, and a dynamic import with a literal target). A dynamic import whose
    target is not a literal cannot be seen, so it refuses unless a ``ring_reason`` (``reason``, else the file's own) is recorded.
    A tighter ring that also fits refuses unless a reason is recorded; a reason with nothing tighter to excuse is noted as stale.
    """
    evidence = _evidence(info, known, name)
    policy = context.policy_for(name)
    if policy is None:
        return AdmissionOutcome(name, ring, False, ("the module has no owning architecture scope",), (), evidence)
    names = [r.name for r in policy.rings]
    if ring not in names:
        return AdmissionOutcome(name, ring, False, (f"unknown ring {ring!r}; this module's rings are {names}",), (), evidence)
    given = _usable_reason(reason) or info.facts.arbor_reason  # a blank or NUL-bearing reason counts as none
    strict = _strict_info(info)
    refusals: list[str] = []
    notes: list[str] = []
    if not fits_ring(context, name, strict, ring, known):
        refusals.append(f"its facts violate ring {ring!r}: an import, external package, effect or abstraction fails that ring's rules")
    unreadable = sorted(line for line, target in info.facts.dynamic_imports if not target)
    if unreadable and given is None:
        lines = ", ".join(map(str, unreadable))
        refusals.append(f"a dynamic import with a non-literal target (line {lines}) cannot be verified; record a ring_reason that names it")
    tighter = next((c for c in names[: names.index(ring)] if fits_ring(context, name, strict, c, known)), None)
    if tighter is not None and given is None:
        refusals.append(f"ring {tighter!r} also fits; use it, or record why {ring!r} is needed as ring_reason")
    if given is not None and tighter is None and not unreadable:
        notes.append("ring_reason is recorded but no tighter ring fits and nothing is unverifiable: the reason may be stale")
    return AdmissionOutcome(name, ring, not refusals, tuple(refusals), tuple(notes), evidence)


def admission_warnings(
    context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str], name: str, ring: str,
) -> tuple[str, ...]:
    """#161: importers of ``name`` whose own ring may not import ``ring`` (``fits_ring`` judges outgoing edges only; ``check`` fails on these later)."""
    policy = context.policy_for(name)
    if policy is None:
        return ()
    found = sorted(
        importer for importer, targets in _internal_targets(context, modules, known).items()
        if name in targets and (importer_ring := context.resolve_ring(importer)) is not None
        and ring not in policy.allowed_rings(importer_ring)
    )
    return tuple(f"{importer} [{context.resolve_ring(importer)}] imports it and may not import ring {ring!r}" for importer in found)


def audit_classification(
    context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str], taint: TaintResult | None = None,
) -> list[Violation]:
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
        subject = _Subject(context, policy, name, info, ring, targets, importers, taint or TaintResult())
        found = (_inner_ring(subject, known), _composition_shaped(subject), _program_and_library(subject))
        out += [v for v in found if v is not None]
    return out
