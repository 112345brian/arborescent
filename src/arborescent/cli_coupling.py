"""``arborescent coupling`` (#148): fan-in, entrypoint closure, ambient hubs, the ratchet and the cost of a new module.

``report``, ``startup`` and ``cost`` read only; ``check`` fails when a hub's importer count or an entrypoint's closure grew
past the baseline, a ``closure_max`` budget is exceeded or a lazy mount loads eagerly (#216); ``baseline`` lowers it and
records any growth with a reason. Only ``startup --measure`` runs project code: it imports each entrypoint in a subprocess
under ``-X importtime`` and reports the time and the heavy third-party imports.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import coupling_ratchet as ratchet
from .coupling import NewModule, ambient_hubs, cost_of_new_modules, mount_cost
from .coupling_measure import (
    HEAVY_MS,
    TIMEOUT_SECONDS,
    ImportRun,
    Measure,
    MeasureSettings,
    Measuring,
    blame_lines,
    document,
    eager_mounts,
    load_baseline,
    measure_settings,
    measured_lines,
    run_importtime,
    startup_failures,
)
from .domain import PolicyError
from .ports import FileSystem, ToolRunner

__all__ = ["Interpreter", "add_coupling_parser", "run_coupling"]

_ACTIONS = {
    "report": "fan-in, entrypoint closure sizes and ambient-hub candidates",
    "check": "fail on growth past the baseline, an exceeded closure budget or an eagerly loaded lazy mount",
    "startup": "per entrypoint: closure size and budget, the heaviest imports, and what each lazy mount adds (--measure times them)",
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
    startup = actions.choices["startup"]
    startup.add_argument("--top", type=int, default=5, help="how many imports to blame (default 5)")
    startup.add_argument(
        "--measure", action="store_true",
        help="RUNS the project: import each entrypoint under `python -X importtime` and report time and heavy third-party imports",
    )
    startup.add_argument("--threshold-ms", type=float, help=f"with --measure: how slow a third-party package is heavy (default {HEAVY_MS:g})")
    startup.add_argument("--python", help="with --measure: the interpreter that has the project's dependencies (default: this one)")
    startup.add_argument("--timeout", type=float, help=f"with --measure: seconds per entrypoint (default {TIMEOUT_SECONDS:g})")
    cost = actions.choices["cost"]
    cost.add_argument("importer", help="the module that will import the new modules")
    cost.add_argument("--imports", nargs="*", default=[], metavar="MODULE", help="modules the new modules import")
    cost.add_argument("--modules", type=int, default=3, help="how many modules are added (default 3: port, service, adapter)")


@dataclass(frozen=True)
class Interpreter:
    """What ``startup --measure`` runs with: the process runner and the Python used unless ``--python`` names another."""

    runner: ToolRunner
    default: str


def run_coupling(args: argparse.Namespace, measure: Measure, root: Path, fs: FileSystem, interpreter: Interpreter) -> int:
    path = root / measure.policy.coupling.baseline
    action = args.coupling_action
    if action == "report":
        return _report(measure, args.top, args.json)
    if action == "cost":
        return _cost(measure, NewModule(args.importer, tuple(args.imports), args.modules))
    if action == "baseline":
        return _baseline(measure, fs, path, args)
    if action == "startup":
        return _startup(measure, args, root, fs, interpreter)
    return _check(measure, fs, path)


def _startup(measure: Measure, args: argparse.Namespace, root: Path, fs: FileSystem, interpreter: Interpreter) -> int:
    python = args.python
    if python is not None and len(Path(python).parts) > 1:  # relative to where the command was typed, not to the project
        python = str(Path(python).absolute())
    settings = measure_settings(args.measure, MeasureSettings(interpreter.default), python, args.timeout, args.threshold_ms)
    run = None if settings is None else Measuring(settings, interpreter.runner, root, fs)
    top = max(args.top, 0)
    failed = False
    if not measure.closures:
        print("no entrypoints declared (`[coupling] entrypoints = [...]`)")
        return 0
    budgets = dict(measure.policy.coupling.closure_max)
    eager = {(mount, entrypoint) for mount, entrypoint, _ in eager_mounts(measure)}
    for closure in measure.closures:
        size = len(closure.modules)
        cap = budgets.get(closure.entrypoint)
        budget = "no budget" if cap is None else f"budget {cap}, {'OVER' if size > cap else 'ok'}"
        print(f"{closure.entrypoint}: {size} project modules ({budget})")
        result = None if run is None else run_importtime(measure, run.runner, run.root, run.settings, closure.entrypoint)
        times = {record.name: record.cumulative_us for record in result.records} if isinstance(result, ImportRun) else {}
        for line in blame_lines(measure, closure.entrypoint, top, times):
            print(f"  {line}")
        _print_mounts(measure, closure.entrypoint, eager)
        if isinstance(result, str):
            print(f"{closure.entrypoint}: measurement failed: {result}")
            failed = True
        elif result is not None and run is not None:
            for line in measured_lines(measure, run.read, closure.entrypoint, result, run.settings.threshold_ms):
                print(line)
    return 1 if failed else 0


def _print_mounts(measure: Measure, entrypoint: str, eager: set[tuple[str, str]]) -> None:
    for mount in measure.policy.coupling.lazy_mounts:
        if mount.module == entrypoint:
            continue
        cost = mount_cost(measure.edges.loading, entrypoint, mount.module)
        state = "EAGER (loaded at import time)" if (mount.name, entrypoint) in eager else f"+{cost} module(s) when run"
        print(f"  lazy mount {mount.name} -> {mount.module}: {state}")


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
    print("entrypoint closure (project modules loaded by module-level imports, parent packages included):")
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
    failures = [*ratchet.regressions(counts, loaded[0]), *startup_failures(measure)]
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
