"""``arborescent baseline report`` (#146): read-only. Clusters, concentration and burn-down of each baseline file, as text or JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from .baseline_history import PERIODS
from .baseline_report import DEFAULT_DEPTH, DEFAULT_THRESHOLD
from .baseline_report_view import FileReport, ReportOptions, as_json, as_text
from .domain import PolicyError

__all__ = ["add_report_parser", "run_report"]

_HISTORY_LIMIT = 200


def add_report_parser(baseline: argparse.ArgumentParser) -> None:
    actions = baseline.add_subparsers(dest="baseline_action")
    r = actions.add_parser("report", help="read-only: what the baseline is made of (clusters, concentration, burn-down)")
    r.add_argument("files", nargs="*", metavar="FILE", help="baseline files relative to the policy root (default: the policy's baseline)")
    r.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="module-family depth: leading segments of a module name or path")
    r.add_argument("--top", type=int, default=10, help="clusters shown per dimension")
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD * 100, help="flag a module, family or target above this percent share")
    r.add_argument("--period", choices=sorted(PERIODS), default="month", help="burn-down period")
    r.add_argument("--history-limit", type=int, default=_HISTORY_LIMIT, help="newest revisions of each file read for the burn-down")
    r.add_argument("--no-history", action="store_true", help="skip the burn-down (no git needed)")
    r.add_argument("--json", action="store_true", help="machine-readable output")


def run_report(
    args: argparse.Namespace, baseline: str, load: Callable[[str, ReportOptions], FileReport],
) -> int:
    """The one effectful step: rendering lives in ``baseline_report_view``; this writes it out."""
    if args.depth < 1 or args.top < 1 or args.history_limit < 1 or not 0 <= args.threshold <= 100:
        raise PolicyError("baseline report: --depth, --top and --history-limit must be at least 1, and --threshold 0 to 100")
    options = ReportOptions(args.depth, args.top, args.threshold, args.period, args.history_limit, history=not args.no_history)
    reports = [load(name, options) for name in (args.files or [baseline])]
    sys.stdout.write(json.dumps({"files": [as_json(r) for r in reports]}, indent=2) + "\n" if args.json else as_text(reports, options))
    return 0
