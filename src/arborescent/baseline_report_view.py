"""Pure assembly and rendering for ``arborescent baseline report`` (#146): one file's analysis and burn-down, as text or JSON.

Split out of ``cli_baseline_report`` so the command module holds only the one effectful step (writing the output); this
module reads through the injected ports and returns values, matching how ``cycles_report``/``cli_cycles`` split.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .baseline_history import Burndown, burndown
from .baseline_report import DIMENSIONS, Analysis, Cluster, analyse, parse_entries
from .domain import PolicyError
from .ports import FileSystem, ToolRunner

__all__ = ["FileReport", "ReportOptions", "as_json", "as_text", "file_report"]


@dataclass(frozen=True)
class ReportOptions:
    depth: int
    top: int
    threshold: float  # percent, 0 to 100
    period: str
    history_limit: int
    history: bool


@dataclass(frozen=True)
class FileReport:
    path: str
    shape: str
    analysis: Analysis
    history: Burndown | str | None  # a note when there is no history, None when it was not asked for


def file_report(name: str, options: ReportOptions, root: Path, fs: FileSystem, runner: ToolRunner) -> FileReport:
    path = root / name
    if not fs.is_file(path):
        raise PolicyError(f"baseline report: {name} does not exist")
    try:
        shape, entries = parse_entries(fs.read_json(path))
    except ValueError as exc:
        raise PolicyError(f"baseline report: {name}: {exc}") from exc
    analysis = analyse(entries, options.depth, options.top, options.threshold / 100)
    history = burndown(runner, root, Path(name).as_posix(), options.period, options.history_limit) if options.history else None
    return FileReport(name, shape, analysis, history)


def _cluster_json(c: Cluster) -> dict[str, object]:
    return {"key": c.key, "count": c.count, "share": round(c.share, 4)}


def as_json(r: FileReport) -> dict[str, object]:
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


def as_text(reports: list[FileReport], options: ReportOptions) -> str:
    lines: list[str] = []
    for r in reports:
        a = r.analysis
        lines.append(f"{r.path}: {a.total} total, {a.entries} entr{'y' if a.entries == 1 else 'ies'} (shape: {r.shape})")
        for name in DIMENSIONS:
            lines.append(f"  by {name}:")
            lines.extend(f"    {c.count:>6}  {c.share:6.1%}  {c.key}" for c in a.clusters[name])
            if not a.clusters[name]:
                lines.append("    none")
        lines.append(f"  concentration (above {options.threshold:g}%): " + ("none" if not a.concentration else ""))
        lines.extend(f"    CONCENTRATED {name} {c.key}: {c.count} ({c.share:.1%}); one fix may clear many" for name, c in a.concentration)
        lines.extend(_history_lines(r.history, options.period))
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
