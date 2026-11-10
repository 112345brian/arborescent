"""``arborescent baseline report`` (#146): read-only. Clusters, concentration and burn-down of each baseline file, as text or JSON."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .baseline_history import PERIODS, Burndown, burndown
from .baseline_report import (
    DEFAULT_DEPTH,
    DEFAULT_THRESHOLD,
    DIMENSIONS,
    Analysis,
    Cluster,
    analyse,
    parse_entries,
)
from .domain import Policy, PolicyError
from .engine import Services

__all__ = ["add_report_parser", "run_report"]

_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class _Report:
    path: str
    shape: str
    analysis: Analysis
    history: Burndown | str | None  # a note when there is no history, None when it was not asked for


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


def run_report(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    if args.depth < 1 or args.top < 1 or args.history_limit < 1 or not 0 <= args.threshold <= 100:
        raise PolicyError("baseline report: --depth, --top and --history-limit must be at least 1, and --threshold 0 to 100")
    reports = [_report(name, args, root, svc) for name in (args.files or [policy.baseline])]
    sys.stdout.write(json.dumps({"files": [_as_json(r) for r in reports]}, indent=2) + "\n" if args.json else _as_text(reports, args))
    return 0


def _report(name: str, args: argparse.Namespace, root: Path, svc: Services) -> _Report:
    path = root / name
    if not svc.fs.is_file(path):
        raise PolicyError(f"baseline report: {name} does not exist")
    try:
        shape, entries = parse_entries(svc.fs.read_json(path))
    except ValueError as exc:
        raise PolicyError(f"baseline report: {name}: {exc}") from exc
    analysis = analyse(entries, args.depth, args.top, args.threshold / 100)
    history = None if args.no_history else burndown(svc.runner, root, Path(name).as_posix(), args.period, args.history_limit)
    return _Report(name, shape, analysis, history)


def _cluster_json(c: Cluster) -> dict[str, object]:
    return {"key": c.key, "count": c.count, "share": round(c.share, 4)}


def _as_json(r: _Report) -> dict[str, object]:
    history: dict[str, object] | None = None
    if isinstance(r.history, Burndown):
        history = {
            "commits": r.history.commits,
            "truncated": r.history.truncated,
            "only_grows": r.history.only_grows,
            "periods": [{"period": p.label, "added": p.added, "removed": p.removed, "net": p.net, "total": p.total} for p in r.history.periods],
        }
    return {
        "path": r.path,
        "shape": r.shape,
        "total": r.analysis.total,
        "entries": r.analysis.entries,
        "clusters": {name: [_cluster_json(c) for c in r.analysis.clusters[name]] for name in DIMENSIONS},
        "concentration": [{"dimension": name, **_cluster_json(c)} for name, c in r.analysis.concentration],
        "burn_down": history,
        "burn_down_note": r.history if isinstance(r.history, str) else None,
    }


def _as_text(reports: list[_Report], args: argparse.Namespace) -> str:
    lines: list[str] = []
    for r in reports:
        a = r.analysis
        lines.append(f"{r.path}: {a.total} total, {a.entries} entr{'y' if a.entries == 1 else 'ies'} (shape: {r.shape})")
        for name in DIMENSIONS:
            lines.append(f"  by {name}:")
            lines.extend(f"    {c.count:>6}  {c.share:6.1%}  {c.key}" for c in a.clusters[name])
            if not a.clusters[name]:
                lines.append("    none")
        lines.append(f"  concentration (above {args.threshold:g}%): " + ("none" if not a.concentration else ""))
        lines.extend(f"    CONCENTRATED {name} {c.key}: {c.count} ({c.share:.1%}); one fix may clear many" for name, c in a.concentration)
        lines.extend(_history_lines(r.history, args.period))
    return "\n".join(lines) + "\n"


def _history_lines(history: Burndown | str | None, period: str) -> list[str]:
    if history is None:
        return []
    if isinstance(history, str):
        return [f"  burn-down: {history}"]
    note = "; older revisions beyond the limit were not read" if history.truncated else ""
    lines = [f"  burn-down by {period} ({history.commits} revision(s){note}):", f"    {'period':<10} {'added':>6} {'removed':>8} {'net':>6} {'total':>6}"]
    lines.extend(f"    {p.label:<10} {p.added:>6} {p.removed:>8} {p.net:>+6} {p.total:>6}" for p in history.periods)
    if history.only_grows:
        lines.append("    WARNING this baseline has only ever grown")
    return lines
