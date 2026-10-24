"""ArchLint's own checks, as pure functions over already-parsed modules.

Rules: ``unclassified``, ``stale-classification``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import ALL, Policy, Violation, dist_name
from .facts import STDLIB, ModuleInfo, resolve_targets

__all__ = ["RULES", "check_modules"]

RULES = (
    "unclassified",
    "internal-import",
    "external-import",
    "os-branching",
    "bad-import",
    "undeclared-entry-dependency",
)


def check_modules(
    policy: Policy,
    modules: Mapping[str, ModuleInfo],
    known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None],
) -> list[Violation]:
    tops = {m.split(".")[0] for m in known}
    out: list[Violation] = []
    third_party: dict[str, set[str]] = {}  # distribution -> importing modules
    for name, info in sorted(modules.items()):
        ring = policy.ring_of(name)
        if ring is None:
            out.append(Violation("unclassified", name, "module matches no [classify] pattern; every module needs a ring"))
            continue
        rule = policy.ring(ring)
        allowed = policy.allowed_rings(ring)
        for imp in info.facts.imports:
            internal, external, error = resolve_targets(name, info.is_package, imp, known, tops)
            if error:
                out.append(Violation("bad-import", name, error, imp.line))
            for target in filter(None, internal):
                target_ring = policy.ring_of(target)
                if target_ring is not None and target_ring not in allowed:
                    out.append(
                        Violation(
                            "internal-import",
                            name,
                            f"[{ring}] imports {target} [{target_ring}]; {ring} may import only {sorted(allowed)}",
                            imp.line,
                        )
                    )
            for top in external:
                if top not in STDLIB:
                    third_party.setdefault(dist_name(policy.dependencies.get(top, top)), set()).add(name)
                if ALL not in rule.external and top not in rule.external:
                    out.append(
                        Violation(
                            "external-import",
                            name,
                            f"[{ring}] imports external package {top!r}; {ring} may import only {sorted(rule.external)}",
                            imp.line,
                        )
                    )
        if not rule.os_branching:
            out += [
                Violation("os-branching", name, f"[{ring}] references {attr}; only permitted rings may branch on the OS", line)
                for line, attr in info.facts.os_uses
            ]
    for script in policy.entry_scripts:
        declared = entry_deps.get(script)
        if declared is None:
            if script in known:
                out.append(Violation("undeclared-entry-dependency", script, "entry script has no `# /// script` block"))
            continue
        have = {dist_name(d) for d in declared}
        for dist in sorted(set(third_party) - have):
            out.append(Violation("undeclared-entry-dependency", script, f"does not declare {dist!r} (imported by project code)"))
    return out
