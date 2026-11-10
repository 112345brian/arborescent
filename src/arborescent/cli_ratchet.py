"""``arborescent ratchet`` (#147): record or lower the named ratchets' baselines, and check their growth since a git ref."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import ratchets as ratchets_mod
from .domain import Policy, PolicyError, RatchetSpec
from .engine import EngineRun, Services, run_own
from .ratchet_measure import Subject

__all__ = ["add_ratchet_parser", "growth_run", "run_ratchet"]


def add_ratchet_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    r = sub.add_parser("ratchet", help="record or lower the [ratchet.<name>] baselines; a rise must be explicit")
    r.add_argument("--name", help="only this ratchet (default: every configured one)")
    r.add_argument("--allow-growth", action="store_true", help="allow a count to rise, with a required reason and approver")
    r.add_argument("--reason", help="why the count is allowed to rise")
    r.add_argument("--approved-by", metavar="WHO", help="who approved the rise: a name or issue URL")


def run_ratchet(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> list[str]:
    """Record the chosen ratchets and return what was recorded, one line each."""
    chosen = _chosen(policy, args.name)
    found, scan = run_own(policy, root, svc)
    subject = Subject(root, svc.fs, svc.runner, {m: scan.paths[m] for m in scan.modules if m in scan.paths}, found)
    try:
        return ratchets_mod.record(chosen, subject, allow_growth=args.allow_growth, reason=args.reason, approved_by=args.approved_by)
    except ValueError as exc:  # a measure that cannot run, or a baseline file that cannot be read
        raise PolicyError(str(exc)) from exc


def _chosen(policy: Policy, name: str | None) -> list[RatchetSpec]:
    if not policy.ratchets:
        raise PolicyError("no `[ratchet.<name>]` is configured")
    chosen = [spec for spec in policy.ratchets if name in (None, spec.name)]
    if not chosen:
        raise PolicyError(f"unknown ratchet {name!r}; configured: {[spec.name for spec in policy.ratchets]}")
    return chosen


def growth_run(ref: str, policy: Policy, root: Path, svc: Services) -> EngineRun:
    started = svc.clock.now()
    problems = ratchets_mod.growth_against(ref, policy, root, svc.fs, svc.runner)
    lines = problems or [f"ratchet baselines match their growth entries since {ref}"]
    return EngineRun("ratchet growth", "fail" if problems else "ok", svc.clock.now() - started, lines)
