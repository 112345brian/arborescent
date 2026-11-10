"""``arborescent coupling`` (#148): fan-in, entrypoint closure, ambient hubs, the ratchet and the cost of a new module.

``report`` and ``cost`` read only; ``check`` fails when a hub's importer count or an entrypoint's closure grew past the
baseline; ``baseline`` lowers it and records any growth with a reason.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import coupling_ratchet as ratchet
from .coupling import (
    Closure,
    FanIn,
    Hub,
    ImportEdges,
    NewModule,
    ambient_hubs,
    cost_of_new_modules,
    entrypoint_closure,
    fan_in,
    import_edges,
)
from .domain import Policy, PolicyError
from .engine import Services
from .ports import FileSystem, ScanResult

__all__ = ["add_coupling_parser", "run_coupling"]

_ACTIONS = {
    "report": "fan-in, entrypoint closure sizes and ambient-hub candidates",
    "check": "fail when a hub's importers or an entrypoint's closure grew past the baseline",
    "baseline": "record or lower the coupling baseline; growth needs --allow-growth and --reason",
    "cost": "modules a new port, service and adapter would add to each entrypoint's closure",
}


def add_coupling_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    coupling = sub.add_parser("coupling", help="fan-in and entrypoint closure metrics, ratchet and cost of a new module")
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


class _Measure:
    """Everything one scan says about coupling; built once per command."""

    def __init__(self, policy: Policy, scan: ScanResult) -> None:
        self.policy = policy
        self.scan = scan
        self.edges: ImportEdges = import_edges(scan.modules, scan.known)
        self.rows: list[FanIn] = fan_in(self.edges, scan.modules)
        missing = [name for name in policy.coupling.entrypoints if name not in scan.known]
        if missing:
            raise PolicyError(f"`coupling.entrypoints` names modules that do not exist: {missing}")
        self.closures: list[Closure] = [entrypoint_closure(self.edges, name) for name in policy.coupling.entrypoints]

    def ring(self, module: str) -> str | None:
        scope = self.scan.scope_by_module.get(module)
        local = self.policy.scope(scope).policy if scope is not None and self.policy.scopes else self.policy
        return local.resolve_ring(self.scan.arbor_index, module)

    def over_cap(self, row: FanIn) -> int | None:
        cap = dict(self.policy.coupling.fan_in_max).get(self.ring(row.module) or "")
        return cap if cap is not None and row.direct > cap else None

    def counts(self) -> dict[str, dict[str, int]]:
        return ratchet.measured(self.policy.coupling, self.rows, self.closures)


def run_coupling(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    measure = _Measure(policy, svc.scanner.scan(policy, root))
    path = root / policy.coupling.baseline
    action = args.coupling_action
    if action == "report":
        return _report(measure, args.top, args.json)
    if action == "cost":
        return _cost(measure, NewModule(args.importer, tuple(args.imports), args.modules))
    if action == "baseline":
        return _baseline(measure, svc.fs, path, args)
    return _check(measure, svc.fs, path)


def _report(measure: _Measure, top: int, as_json: bool) -> int:
    hubs = ambient_hubs(measure.rows, measure.scan.modules, measure.policy.coupling.hub_fan_in)
    if as_json:
        print(json.dumps(_document(measure, hubs), indent=2))
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


def _document(measure: _Measure, hubs: list[Hub]) -> dict[str, object]:
    return {
        "fan_in": [
            {"module": r.module, "direct": r.direct, "module_level": r.runtime, "transitive": r.transitive, "ring": measure.ring(r.module)}
            for r in measure.rows
        ],
        "closures": [
            {"entrypoint": c.entrypoint, "modules": len(c.modules), "function_level_only": len(c.lazy_only), "type_only": len(c.type_only)}
            for c in measure.closures
        ],
        "hubs": [
            {"module": h.module, "importers": h.direct, "accessors": list(h.accessors), "public": h.public}
            for h in hubs
        ],
    }


def _cost(measure: _Measure, new: NewModule) -> int:
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


def _load(fs: FileSystem, path: Path) -> tuple[ratchet.Counts, list[object]] | None:
    if not fs.exists(path):
        return None
    try:
        return ratchet.parse(fs.read_json(path))
    except ValueError as exc:
        raise PolicyError(f"{path.name}: {exc}") from exc


def _check(measure: _Measure, fs: FileSystem, path: Path) -> int:
    loaded = _load(fs, path)
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


def _baseline(measure: _Measure, fs: FileSystem, path: Path, args: argparse.Namespace) -> int:
    try:
        counts = measure.counts()
        document = ratchet.next_document(_load(fs, path), counts, args.allow_growth, args.reason)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    fs.write_json(path, document)
    print(f"coupling baseline: recorded {sum(len(table) for table in counts.values())} count(s) in {path.name}")
    return 0
