"""``arborescent coupling`` (#148): fan-in, entrypoint closure, ambient hubs, the ratchet and the cost of a new module.

``report`` and ``cost`` read only; ``check`` fails when a hub's importer count or an entrypoint's closure grew past the
baseline; ``baseline`` lowers it and records any growth with a reason.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from . import coupling_ratchet as ratchet
from .coupling import NewModule, ambient_hubs, cost_of_new_modules
from .coupling_measure import Measure, document, load_baseline
from .domain import PolicyError
from .ports import FileSystem

__all__ = ["add_coupling_parser", "run_coupling"]

_ACTIONS = {
    "report": "fan-in, entrypoint closure sizes and ambient-hub candidates",
    "check": "fail when a hub's importers or an entrypoint's closure grew past the baseline",
    "baseline": "record or lower the coupling baseline; growth needs --allow-growth and --reason",
    "cost": "modules a new port, service and adapter would add to each entrypoint's closure",
}


def add_coupling_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    coupling = add_parser("coupling", help="fan-in and entrypoint closure metrics, ratchet and cost of a new module")
    actions = coupling.add_subparsers(dest="coupling_action", required=True)
    for name, summary in _ACTIONS.items():
        actions.add_parser(name, help=summary)
    report = actions.choices["report"]
    report.add_argument("--top", type=int, default=15, help="how many modules to list (default 15)")
    report.add_argument("--json", action="store_true", help="print the report as JSON")
    base = actions.choices["baseline"]
    base.add_argument("--allow-growth", action="store_true", help="allow counts to rise or new entries, with a --reason")
    base.add_argument("--reason", help="why the baseline grows")
    cost = actions.choices["cost"]
    cost.add_argument("importer", help="the module that will import the new modules")
    cost.add_argument("--imports", nargs="*", default=[], metavar="MODULE", help="modules the new modules import")
    cost.add_argument("--modules", type=int, default=3, help="how many modules are added (default 3: port, service, adapter)")


def run_coupling(args: argparse.Namespace, measure: Measure, root: Path, fs: FileSystem) -> int:
    path = root / measure.policy.coupling.baseline
    action = args.coupling_action
    if action == "report":
        return _report(measure, args.top, args.json)
    if action == "cost":
        return _cost(measure, NewModule(args.importer, tuple(args.imports), args.modules))
    if action == "baseline":
        return _baseline(measure, fs, path, args)
    return _check(measure, fs, path)


def _report(measure: Measure, top: int, as_json: bool) -> int:
    hubs = ambient_hubs(measure.rows, measure.scan.modules, measure.policy.coupling.hub_fan_in)
    if as_json:
        print(json.dumps(document(measure, hubs), indent=2))
        return 0
    print(f"fan-in (distinct importing modules), top {top}:")
    for row in measure.rows[:top]:
        cap = measure.over_cap(row)
        flag = "" if cap is None else f"  OVER CAP {cap} for ring {measure.ring(row.module)}"
        print(f"  {row.module}: direct {row.direct}, module-level {row.runtime}, transitive {row.transitive}{flag}")
    print("entrypoint closure (project modules loaded by module-level imports):")
    for closure in measure.closures:
        print(f"  {closure.entrypoint}: {len(closure.modules)} (function-level only +{len(closure.lazy_only)}, type-only +{len(closure.type_only)})")
    if not measure.closures:
        print("  no entrypoints declared (`[coupling] entrypoints = [...]`)")
    print("ambient hub candidates (many importers, public surface mostly state or I/O accessors):")
    for hub in hubs:
        print(f"  {hub.module}: {hub.direct} importers, {len(hub.accessors)} of {hub.public} public definitions use an effect: {', '.join(hub.accessors)}")
    if not hubs:
        print("  none")
    return 0


def _cost(measure: Measure, new: NewModule) -> int:
    if new.importer not in measure.scan.known:
        raise PolicyError(f"{new.importer!r} is not a module of this repository")
    unknown = [name for name in new.imports if name not in measure.scan.known]
    if unknown:
        raise PolicyError(f"--imports names modules that do not exist: {unknown}")
    added = cost_of_new_modules(measure.closures, measure.edges, new)
    print(f"adding {new.count} module(s) imported by {new.importer}:")
    for entrypoint, extra in sorted(added.items()):
        size = len(next(c for c in measure.closures if c.entrypoint == entrypoint).modules)
        print(f"  {entrypoint}: closure {size} -> {size + extra} (+{extra})")
    if not added:
        print("  no declared entrypoint loads that module, so no closure grows")
    return 0


def _check(measure: Measure, fs: FileSystem, path: Path) -> int:
    loaded = load_baseline(fs, path)
    if loaded is None:
        raise PolicyError(f"no coupling baseline at {path.name}; create it with `arborescent coupling baseline`")
    counts = measure.counts()
    failures = ratchet.regressions(counts, loaded[0])
    for line in failures:
        print(f"FAIL   {line}")
    for line in ratchet.lowerable(counts, loaded[0]):
        print(f"STALE  {line} (lower it: `arborescent coupling baseline`)")
    print(f"arborescent coupling check: {'FAILED' if failures else 'OK'} ({len(failures)} regression(s))")
    return 1 if failures else 0


def _baseline(measure: Measure, fs: FileSystem, path: Path, args: argparse.Namespace) -> int:
    try:
        counts = measure.counts()
        recorded = ratchet.next_document(load_baseline(fs, path), counts, args.allow_growth, args.reason)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    fs.write_json(path, recorded)
    print(f"coupling baseline: recorded {sum(len(table) for table in counts.values())} count(s) in {path.name}")
    return 0
