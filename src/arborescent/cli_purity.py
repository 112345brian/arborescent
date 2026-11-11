"""``arborescent purity``: every mixed module, ranked by blast radius, with what to split out of it (#173)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .domain import Policy
from .engine import Services
from .import_cycles import import_edges
from .purity import ModulePurity, assess_in_ring, by_ring, is_allowed, summarize
from .rules import RuleContext

_HEADER = "mixed modules, most imported first (a module's importers are the ones that inherit its effects):"


def ranked(policy: Policy, root: Path, svc: Services) -> list[tuple[ModulePurity, str, bool]]:
    """(report, ring, allowed) for each mixed module under a ring: importers descending, then more pure units, then name."""
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
        report = assess_in_ring(scan.modules[name], local, ring) if local is not None else None
        if local is not None and report is not None:
            rows.append((replace(report, importers=len(importers.get(name, ()))), ring, is_allowed(local.mixed_purity, name)))
    return sorted(rows, key=lambda row: (-row[0].importers, -len(row[0].pure), row[0].module))


def run_purity(policy: Policy, root: Path, svc: Services, as_json: bool, top: int | None = None) -> int:
    """Report only: the ``mixed-purity`` rule under ``check`` is what fails a run. ``top`` trims the listing, never the findings."""
    if top is not None and top < 1:
        print(f"--top must be a positive number, got {top}", file=sys.stderr)
        return 2
    everything = ranked(policy, root, svc)
    rows = everything[:top]
    if as_json:
        print(json.dumps([{**asdict(report), "ring": ring, "allowed": allowed} for report, ring, allowed in rows], indent=2))
        return 0
    if not rows:
        print("no mixed modules")
        return 0
    totals = {ring: sum(1 for row in everything if row[1] == ring) for _, ring, _ in everything}
    print(_HEADER)
    for ring, group in by_ring(rows):
        print(f"[{ring}] {totals[ring]} mixed module(s)" + (f", showing {len(group)}" if len(group) != totals[ring] else ""))
        for report, _, allowed in group:
            print(f"  {report.module}:{report.line}{' (allowed)' if allowed else ''}{summarize(report)}")
    print(f"{len(everything)} mixed module(s)" if len(rows) == len(everything) else f"showing {len(rows)} of {len(everything)} mixed module(s)")
    return 0
