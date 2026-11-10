"""``arborescent cycles``: the import cycles, which edges to cut, and which function-level imports only break a cycle (#149)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .domain import Policy
from .engine import Services
from .import_cycles import Cycle, EdgeKind, ImportEdge, find_cycles, import_edges
from .ports import ScanResult
from .rules import RuleContext


def _rings(context: RuleContext, scan: ScanResult) -> dict[str, int]:
    """Each classified module's ring, innermost first, as an index into its own scope's ring list."""
    rank: dict[str, int] = {}
    for name in scan.modules:
        ring = context.resolve_ring(name)
        local = context.policy_for(name)
        if ring is not None and local is not None:
            rank[name] = [item.name for item in local.rings].index(ring)
    return rank


def _edge_text(edge: ImportEdge) -> str:
    lines = ", ".join(str(line) for line in edge.lines)
    return f"{edge.source} -> {edge.target} [{edge.kind.value}] line {lines}"


def _cycle_lines(number: int, cycle: Cycle) -> list[str]:
    lines = [f"cycle {number}: {len(cycle.modules)} module(s): {', '.join(cycle.modules)}"]
    lines.append(f"  edges ({len(cycle.edges)}):")
    lines.extend(f"    {_edge_text(edge)}" for edge in cycle.edges)
    lines.append(f"  cut {len(cycle.cut)} edge(s) to make it acyclic (approximate minimum):")
    lines.extend(f"    {_edge_text(edge)}" for edge in cycle.cut)
    if cycle.cycle_breaking:
        lines.append("  function-level imports that exist to break this cycle (module level would close an import-time cycle):")
        lines.extend(f"    {_edge_text(edge)}" for edge in cycle.cycle_breaking)
    lines.append(
        f"  invert with a Protocol owned by {cycle.protocol_owner}, the lowest-ring module here: "
        "the other modules depend on it and the importer registers its implementation"
    )
    return lines


def render_cycles(cycles: Sequence[Cycle], edges: Sequence[ImportEdge]) -> list[str]:
    function_level = [edge for edge in edges if edge.kind is EdgeKind.LAZY]
    inside = {(edge.source, edge.target) for cycle in cycles for edge in cycle.edges}
    on_cycle = sum(1 for edge in function_level if (edge.source, edge.target) in inside)
    breaking = sum(len(cycle.cycle_breaking) for cycle in cycles)
    if not cycles:
        return [f"no import cycles ({len(function_level)} function-level import edge(s), none on a cycle)"]
    lines = [f"{len(cycles)} import cycle(s)"]
    for number, cycle in enumerate(cycles, start=1):
        lines.extend(_cycle_lines(number, cycle))
    lines.append(
        f"function-level import edges: {len(function_level)}, {on_cycle} on a cycle ({breaking} exist to break one), "
        f"{len(function_level) - on_cycle} outside every cycle"
    )
    return lines


def run_cycles(policy: Policy, root: Path, svc: Services) -> int:
    """Report only: the ``circular-import`` rule under ``check`` is what fails a run."""
    scan = svc.scanner.scan(policy, root)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    rank = _rings(context, scan)
    classified = {name: info for name, info in scan.modules.items() if name in rank and name not in context.tool_modules}
    edges = import_edges(classified, scan.known, context.scope_name)
    for line in render_cycles(find_cycles(edges, rank), edges):
        print(line)
    return 0
