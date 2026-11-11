"""``arborescent purity``: every mixed module, ranked by blast radius, with what to split out of it (#173)."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

from .domain import Policy
from .engine import Services
from .import_cycles import import_edges
from .purity import ModulePurity, assess, is_allowed, summarize
from .rules import RuleContext

_HEADER = "mixed modules, most imported first (a module's importers are the ones that inherit its effects):"


def ranked(policy: Policy, root: Path, svc: Services) -> list[tuple[ModulePurity, str, bool]]:
    """(report, ring, allowed) for each mixed module under a ring, importers descending, then more effectful definitions, then name."""
    scan = svc.scanner.scan(policy, root)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    rings = {name: ring for name in scan.modules if name not in context.tool_modules and (ring := context.resolve_ring(name)) is not None}
    edges = import_edges({name: scan.modules[name] for name in rings}, scan.known, context.scope_name)
    importers: dict[str, set[str]] = {}
    for edge in edges:
        importers.setdefault(edge.target, set()).add(edge.source)
    rows: list[tuple[ModulePurity, str, bool]] = []
    for name, ring in rings.items():
        local = context.policy_for(name)
        report = assess(scan.modules[name], local) if local is not None else None
        if local is not None and report is not None:
            rows.append((replace(report, importers=len(importers.get(name, ()))), ring, is_allowed(local.mixed_purity, name)))
    return sorted(rows, key=lambda row: (-row[0].importers, -len(row[0].effectful), row[0].module))


def run_purity(policy: Policy, root: Path, svc: Services, as_json: bool) -> int:
    """Report only: the ``mixed-purity`` rule under ``check`` is what fails a run."""
    rows = ranked(policy, root, svc)
    if as_json:
        print(json.dumps([{**asdict(report), "ring": ring, "allowed": allowed} for report, ring, allowed in rows], indent=2))
        return 0
    if not rows:
        print("no mixed modules")
        return 0
    print(_HEADER)
    for report, ring, allowed in rows:
        print(f"  {report.module} [{ring}]{' (allowed)' if allowed else ''}:{report.line}{summarize(report)}")
    print(f"{len(rows)} mixed module(s)")
    return 0
