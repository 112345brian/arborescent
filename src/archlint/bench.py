"""``archlint bench``: measure, don't assert. Cold and warm wall-clock for every engine and for the whole ``check``
on codebases you prepared (each directory holds an ``archlint.toml``). Prints a markdown table.

"cold" = no ArchLint cache (and the first run of an external tool); "warm" = the immediately following run.
External tools keep their own caches or the OS file cache; the numbers say what a developer would see, not what
the algorithms cost.
"""

from __future__ import annotations

import dataclasses
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import load_policy
from .engine import Options, Services, run_check, run_own
from .generate import IMPORT_LINTER_FILE, generate
from .ports import FileSystem

__all__ = ["run_bench"]


def _clear(fs: FileSystem, root: Path) -> None:
    fs.remove_tree(root / ".archlint")


def bench_one(root: Path, names: Sequence[str], make: Callable[[Path], Services]) -> list[str]:
    svc = make(root)
    fs = svc.fs
    policy, _ = load_policy(fs, root / "archlint.toml")
    policy = dataclasses.replace(policy, type_checkers=tuple(names))
    rows: list[tuple[str, str, str, str]] = []  # engine, cold, warm, note

    def twice(label: str, fn: Callable[[], str], reset: Callable[[], None] | None = None) -> None:
        if reset:
            reset()
        t0 = time.perf_counter()
        note = fn()
        cold = time.perf_counter() - t0
        t0 = time.perf_counter()
        fn()
        warm = time.perf_counter() - t0
        rows.append((label, f"{cold:.2f}", f"{warm:.2f}", note))
        print(f"  [{root.name}] {label}: cold {cold:.2f}s warm {warm:.2f}s {note}", file=sys.stderr, flush=True)

    scan = svc.scanner.scan(policy, root)
    n_files = len(scan.known)
    _clear(fs, root)

    def own() -> str:
        found, s = run_own(policy, root, svc, practices=False)
        return f"{len(found)} violations, {s.parsed} parsed"

    twice("archlint own checks (parse+rules)", own, lambda: _clear(fs, root))
    chosen = [c for c in svc.checkers if c.name in names]
    twice("generate (in memory)", lambda: f"{len(generate(policy, sorted(scan.known), chosen))} files")
    for rel, text in generate(policy, sorted(scan.known), chosen).items():
        fs.write_text(root / rel, text)

    def tool(argv: list[str]) -> Callable[[], str]:
        def go() -> str:
            if not svc.runner.available(argv[0]):
                return "not installed"
            r = svc.runner.run(argv, root)
            return f"rc={r.returncode}"

        return go

    twice("import-linter (grimp)", tool(["lint-imports", "--config", IMPORT_LINTER_FILE]))
    twice("tach check", tool(["tach", "check"]))
    for c in chosen:
        fs.write_text(root / c.config_file, c.render_config(policy, sorted(set(policy.sources))))
        twice(f"type checker: {c.name}", tool(c.command([])))
    for c in chosen[:1]:
        fs.write_text(root / c.config_file, c.render_config(policy, sorted(set(policy.sources))))
    first = names[0]

    def whole() -> str:
        rep = run_check(policy, root, svc, Options(type_checker=first, use_baseline=False))
        slowest = max((e for e in rep.engines), key=lambda e: e.seconds)
        return f"slowest engine: {slowest.name} {slowest.seconds:.2f}s; sum of engines {sum(e.seconds for e in rep.engines):.2f}s"

    twice(f"WHOLE check (parallel, checker={first})", whole, lambda: _clear(fs, root))
    out = [f"### {root.name}: {n_files} modules", "", "| engine | cold s | warm s | note |", "|---|---:|---:|---|"]
    out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    return out


def run_bench(dirs: Sequence[Path], names: Sequence[str], make: Callable[[Path], Services]) -> int:
    for d in dirs:
        print("\n".join(bench_one(d.resolve(), list(names), make)) + "\n")
    return 0
