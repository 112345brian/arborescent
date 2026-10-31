"""``arborescent packages report``: what crosses the boundaries between the packages the catalog declares.

Read-only. The cut list is the observed imports no ``depends_on`` allows, with the module pairs behind each; packages that
import each other cannot be split until it is empty. Nothing is discovered, planned or migrated here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .domain import Policy, PolicyError
from .engine import Services
from .package_dependencies import dependency_report
from .package_gaps import load_package_context
from .package_report import DependencyReport, PackageEdge

__all__ = ["add_report_parser", "run_report"]

VERSION = 1


def add_report_parser(package_actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    report = package_actions.add_parser(
        "report", help="the imports between the packages in package_catalog: the cut list and the packages that import each other"
    )
    report.add_argument("--all", action="store_true", help="list the evidence of declared imports too, not only the cut list")
    report.add_argument("--json", action="store_true", help="write the report as JSON")
    report.add_argument("--check", action="store_true", help="exit 1 while an import no depends_on allows is left")


def run_report(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    context = load_package_context(policy, root, svc.fs, svc.scanner.scan(policy, root))
    if context is None:
        raise PolicyError("packages report needs package_catalog in the policy (strict preset): it names the declared packages")
    report = dependency_report(context.catalog, context.facts, svc.cycles)
    if args.json:
        print(_json(report), end="")
    else:
        _print(report, [package.id for package in context.catalog.packages], svc, show_all=args.all)
    return 1 if args.check and not report.clean else 0


def _json(report: DependencyReport) -> str:
    document = {
        "version": VERSION,
        "edges": [
            {
                "source": e.source, "target": e.target, "declared": e.declared,
                "uses": [{"module": u.module, "imports": u.imports} for u in e.uses],
            }
            for e in report.edges
        ],
        "cut_list": [{"source": e.source, "target": e.target} for e in report.cut_list],
        "unused": [{"source": s, "target": t} for s, t in report.unused],
        "cycles": None if report.cycles is None else [list(group) for group in report.cycles],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _print(report: DependencyReport, packages: list[str], svc: Services, *, show_all: bool) -> None:
    print(f"package imports: {len(packages)} packages, {len(report.edges)} import edge(s) between them")
    for edge in report.edges:
        count = len(edge.uses)
        print(f"  {edge.source} -> {edge.target}  {count} import(s)  {'declared' if edge.declared else 'UNDECLARED'}")
        if show_all or not edge.declared:
            _print_uses(edge)
    if report.cut_list:
        print(f"cut list: {len(report.cut_list)} edge(s) no depends_on allows; remove these imports, or declare the dependency")
        for edge in report.cut_list:
            print(f"  {edge.source} -> {edge.target}")
    else:
        print("cut list: empty; every import between packages is a declared dependency")
    for source, target in report.unused:
        print(f"declared but never imported: {source} -> {target}")
    _print_cycles(report, svc)


def _print_uses(edge: PackageEdge) -> None:
    for use in edge.uses:
        print(f"      {use.module} imports {use.imports}")


def _print_cycles(report: DependencyReport, svc: Services) -> None:
    if report.cycles is None:
        hint = f" (install {svc.cycles.requirement})" if svc.cycles is not None else ""
        print(f"cycles: not computed; no graph library is available{hint}")
    elif report.cycles:
        print("cycles (these packages import each other, so none of them can be split off alone):")
        for group in report.cycles:
            print(f"  {', '.join(group)}")
    else:
        print("cycles: none")
