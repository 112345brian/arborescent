"""``arborescent bench``: measure, don't assert. Cold and warm wall-clock for every engine and for the whole ``check``
on codebases you prepared (each directory holds an ``arborescent.toml``). Prints a markdown table.

"cold" = no Arborescent cache (and the first run of an external tool); "warm" = the immediately following run.
External tools keep their own caches or the OS file cache; the numbers say what a developer would see, not what
the algorithms cost.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import load_policy
from .domain import Policy
from .engine import (
    Options,
    Services,
    contract_renderers,
    pick_checker,
    run_check,
    run_own,
    write_generated,
)
from .generate import GraphFacts, generate
from .ports import FileSystem

__all__ = ["run_bench"]


def _clear(fs: FileSystem, root: Path) -> None:
    fs.remove_tree(root / ".arborescent")


class _Timer:
    """Times each step twice (cold, then warm), printing progress to stderr and keeping the table rows."""

    def __init__(self, svc: Services, root: Path) -> None:
        self._svc = svc
        self._root = root
        self.rows: list[tuple[str, str, str, str]] = []  # engine, cold, warm, note

    def twice(self, label: str, fn: Callable[[], str], reset: Callable[[], None] | None = None) -> None:
        if reset:
            reset()
        clock = self._svc.clock
        root = self._root
        t0 = clock.now()
        note = fn()
        cold = clock.now() - t0
        t0 = clock.now()
        fn()
        warm = clock.now() - t0
        self.rows.append((label, f"{cold:.2f}", f"{warm:.2f}", note))
        print(f"  [{root.name}] {label}: cold {cold:.2f}s warm {warm:.2f}s {note}", file=sys.stderr, flush=True)


def _tool(svc: Services, root: Path, argv: list[str]) -> Callable[[], str]:
    def go() -> str:
        if not svc.runner.available(argv[0]):
            return "not installed"
        r = svc.runner.run(argv, root)
        return f"rc={r.returncode}"

    return go


def _own_checks(policy: Policy, root: Path, svc: Services) -> Callable[[], str]:
    def own() -> str:
        found, s = run_own(policy, root, svc, practices=False)
        return f"{len(found)} violations, {s.parsed} parsed"

    return own


def _whole_check(policy: Policy, root: Path, svc: Services, first: str) -> Callable[[], str]:
    def whole() -> str:
        rep = run_check(policy, root, svc, Options(type_checker=first, use_baseline=False))
        slowest = max((e for e in rep.engines), key=lambda e: e.seconds)
        return f"slowest engine: {slowest.name} {slowest.seconds:.2f}s; sum of engines {sum(e.seconds for e in rep.engines):.2f}s"

    return whole


def bench_one(root: Path, names: Sequence[str], make: Callable[[Path], Services]) -> list[str]:
    svc = make(root)
    fs = svc.fs
    policy, _ = load_policy(fs, root / "arborescent.toml")
    policy = dataclasses.replace(policy, type_checkers=tuple(names))
    _ = pick_checker(policy, svc)  # an unknown checker name is a PolicyError before anything is timed
    timer = _Timer(svc, root)
    scan = svc.scanner.scan(policy, root)
    _clear(fs, root)
    timer.twice("arborescent own checks (parse+rules)", _own_checks(policy, root, svc), lambda: _clear(fs, root))
    chosen = [c for c in svc.checkers if c.name in names]
    timer.twice(
        "generate (in memory)",
        lambda: f"{len(generate(policy, GraphFacts(sorted(scan.known), scan.arbor_index, scan.scope_by_module), chosen, contract_renderers(svc)))} files",
    )
    _ = write_generated(policy, root, svc)
    for e in svc.engines:
        timer.twice(e.name, _tool(svc, root, e.command([])))
    for c in chosen:
        fs.write_text(root / c.config_file, c.render_config(policy, sorted(set(policy.sources))))
        timer.twice(f"type checker: {c.name}", _tool(svc, root, c.command([])))
    for c in chosen[:1]:
        fs.write_text(root / c.config_file, c.render_config(policy, sorted(set(policy.sources))))
    first = names[0]
    timer.twice(f"WHOLE check (parallel, checker={first})", _whole_check(policy, root, svc, first), lambda: _clear(fs, root))
    out = [f"### {root.name}: {len(scan.known)} modules", "", "| engine | cold s | warm s | note |", "|---|---:|---:|---|"]
    out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in timer.rows]
    return out


def run_bench(dirs: Sequence[Path], names: Sequence[str], make: Callable[[Path], Services]) -> int:
    for d in dirs:
        print("\n".join(bench_one(d.resolve(), list(names), make)) + "\n")
    return 0
