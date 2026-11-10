"""``arborescent admit <module> --ring R``: strict, function-level admission of one module into a ring (#161, stage 1 of the admission design)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from .annotate import insert_annotation
from .classify_audit import admission_warnings, admit_check
from .domain import Policy
from .engine import Services, run_own
from .facts import extract
from .rules import RuleContext


def add_admit_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    admit = add_parser(
        "admit",
        help="admit a module into a ring after a strict function-level scan, then write its __arbor__ annotation (dry-run by default)",
    )
    admit.add_argument("module", help="dotted module name")
    admit.add_argument("--ring", required=True, help="the ring to admit the module into")
    admit.add_argument("--reason", help="record why this ring is needed when a tighter one fits, or a dynamic import cannot be read (written as ring_reason)")
    admit.add_argument("--yes", action="store_true", help="write the annotation instead of printing what would change")


def run_admit(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    _, scan = run_own(policy, root, svc, practices=False)
    info = scan.modules.get(args.module)
    if info is None:
        print(f"admit: {args.module} is not a scanned module")
        return 1
    if info.facts.arbor_error is not None:
        print(f"admit: {info.path}: {info.facts.arbor_error}")
        return 1
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    outcome = admit_check(context, args.module, info, args.ring, scan.known, args.reason)
    print(f"evidence: {outcome.evidence}")
    for note in outcome.notes:
        print(f"note: {note}")
    for warning in admission_warnings(context, scan.modules, scan.known, args.module, args.ring):
        print(f"warning: {warning}")
    if not outcome.admitted:
        for refusal in outcome.refusals:
            print(f"refused: {refusal}")
        return 1
    return _write(args, root, svc, info.path, info.facts.arbor_ring)


def _write(args: argparse.Namespace, root: Path, svc: Services, rel: str, current: str | None) -> int:
    if current == args.ring:
        print(f"admit: {rel} is admitted to {args.ring!r} and already annotated")
        return 0
    if current is not None:
        print(f"refused: {rel} is annotated with ring {current!r}; edit that annotation by hand to move it")
        return 1
    path = root / rel
    updated = insert_annotation(svc.fs.read_text(path), args.ring, args.reason)
    problem = extract(updated).arbor_error
    if problem is not None:
        print(f"refused: --reason is not a usable ring_reason: {problem}")
        return 1
    if not args.yes:
        print(f"would annotate {rel} (ring: {args.ring})")
        return 0
    svc.fs.write_text(path, updated)
    print(f"annotated {rel} (ring: {args.ring})")
    return 0
