"""``arborescent cycles``: the import cycles, which edges to cut, and which function-level imports only break a cycle (#149).

Rendering is pure and lives in ``cycles_report``; this module is the one effectful step, printing its output."""

from __future__ import annotations

from pathlib import Path

from .cycles_report import render_cycles, rings
from .domain import Policy
from .engine import Services
from .import_cycles import find_cycles, import_edges
from .rule_context import RuleContext


def run_cycles(policy: Policy, root: Path, svc: Services) -> int:
    """Report only: the ``circular-import`` rule under ``check`` is what fails a run."""
    scan = svc.scanner.scan(policy, root)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    rank = rings(context, scan)
    classified = {name: info for name, info in scan.modules.items() if name in rank and name not in context.tool_modules}
    edges = import_edges(classified, scan.known, context.scope_name)
    for line in render_cycles(find_cycles(edges, rank), edges):
        print(line)
    return 0
