"""The composition root: wires the adapters to the use cases. ``archlint generate | check | baseline | selftest | bench``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import baseline as ratchet_mod
from .adapters import FsScanner, SubprocessToolRunner
from .config import find_policy, load_policy
from .domain import Policy, PolicyError
from .engine import Options, Services, pick_checker, run_check, run_own, write_generated
from .engines import default_engines
from .selftest import selftest
from .typecheckers import all_checkers

__all__ = ["default_services", "main"]


def default_services(root: Path, cache: bool = True) -> Services:
    scanner = FsScanner(root / ".archlint" if cache else None)
    return Services(scanner, SubprocessToolRunner(), list(all_checkers().values()), default_engines())


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archlint", description="One architecture policy -> import-linter, tach, a type checker.")
    p.add_argument("--config", type=Path, help="archlint.toml or pyproject.toml (default: search upwards)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="write the tool configs compiled from the policy")
    c = sub.add_parser("check", help="drift, engines (parallel), own checks, baseline ratchet")
    c.add_argument("--changed", metavar="REF", help="check only files changed since this git ref")
    c.add_argument("--type-checker", choices=sorted(all_checkers()), help="override the policy's type checker")
    c.add_argument("--no-tools", action="store_true", help="skip import-linter, tach and the type checker")
    c.add_argument("--require-tools", action="store_true", help="a missing engine fails instead of skipping")
    c.add_argument("--no-baseline", action="store_true", help="ignore the baseline: report every violation")
    c.add_argument("--no-cache", action="store_true")
    sub.add_parser("baseline", help="record the current violations (shrink-only ratchet)")
    sub.add_parser("selftest", help="mutation-test the rules")
    b = sub.add_parser("bench", help="time every engine, cold and warm, on prepared codebases")
    b.add_argument("dirs", nargs="+", type=Path, help="directories that each contain an archlint.toml")
    b.add_argument("--checkers", nargs="*", choices=sorted(all_checkers()), default=sorted(all_checkers()))
    return p


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "bench":
        from .bench import run_bench  # imported lazily: only this command needs it

        return run_bench(args.dirs, args.checkers, default_services)
    try:
        policy, root = load_policy(args.config or find_policy(Path.cwd()))
    except PolicyError as exc:
        print(f"archlint: {exc}", file=sys.stderr)
        return 2
    svc = services or default_services(root, cache=not getattr(args, "no_cache", False))
    try:
        return _dispatch(args, policy, root, svc)
    except PolicyError as exc:
        print(f"archlint: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    if args.command == "generate":
        _ = pick_checker(policy, svc)
        for rel in write_generated(policy, root, svc):
            print(f"wrote {rel}")
        return 0
    if args.command == "baseline":
        found, _ = run_own(policy, root, svc)
        print(f"baseline: recorded {ratchet_mod.save(root / policy.baseline, found)} violation(s) in {policy.baseline}")
        return 0
    if args.command == "selftest":
        rep = selftest(policy, root, svc)
        for line in rep.killed:
            print(f"killed    {line}")
        for line in rep.survived:
            print(f"SURVIVED  {line}")
        for line in rep.skipped:
            print(f"skipped   {line}")
        print(f"selftest: {len(rep.killed)} killed, {len(rep.survived)} survived, {len(rep.skipped)} skipped")
        return 0 if rep.ok else 1
    opts = Options(args.changed, args.type_checker, not args.no_tools, args.require_tools, not args.no_baseline)
    report = run_check(policy, root, svc, opts)
    for line in report.drift:
        print(f"DRIFT  {line}")
    shown = report.ratchet.new if report.ratchet and not args.no_baseline else report.violations
    for v in shown:
        print(f"FAIL   {v.render()}")
    if report.ratchet:
        for key in report.ratchet.stale:
            print(f"STALE  baseline entry no longer occurs (delete it: `archlint baseline`): {key}")
    for e in report.engines:
        print(f"{e.status.upper():7}{e.name} ({e.seconds:.2f}s) {' | '.join(e.lines[-1:]) if e.status != 'fail' else ''}")
        if e.status == "fail":
            print("\n".join("        " + ln for ln in e.lines))
    tolerated = report.ratchet.tolerated if report.ratchet else 0
    print(
        f"archlint check: {'OK' if report.ok else 'FAILED'} in {report.seconds:.2f}s "
        f"({len(report.violations)} violation(s), {tolerated} baselined, {report.parsed} file(s) parsed"
        f"{', partial: --changed' if report.partial else ''})"
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
