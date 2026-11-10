"""The burn-down of a baseline (#146): entries added and removed per period, read from the git history of the file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .baseline_report import parse_entries
from .ports import ToolRunner

__all__ = ["PERIODS", "Burndown", "Period", "burndown"]

PERIODS = {"day": 10, "month": 7, "year": 4}  # characters of an ISO date that name the period


@dataclass(frozen=True)
class Period:
    label: str
    added: int
    removed: int
    total: int  # the baseline's size when the period ended

    @property
    def net(self) -> int:
        return self.added - self.removed


@dataclass(frozen=True)
class Burndown:
    periods: list[Period]
    commits: int  # revisions read
    truncated: bool  # older revisions than the limit were not read

    @property
    def only_grows(self) -> bool:
        return any(p.added for p in self.periods) and not any(p.removed for p in self.periods)


def burndown(runner: ToolRunner, root: Path, relative: str, period: str, limit: int) -> Burndown | str:
    """The baseline's history, or the reason there is none (not a repository, not tracked, nothing readable)."""
    listing = runner.run(["git", "log", "--format=%H %cs", f"--max-count={limit + 1}", "--", relative], root)
    if listing.returncode != 0:
        return f"no git history ({(listing.stderr or listing.stdout).strip() or 'git could not run'})"
    commits = [line.split() for line in listing.stdout.splitlines() if len(line.split()) == 2]
    if not commits:
        return "no git history (the file is not tracked)"
    snapshots = _snapshots(runner, root, relative, commits[:limit][::-1])
    if not snapshots:
        return "no git history (no revision of the file could be read)"
    truncated = len(commits) > limit
    return Burndown(_periods(snapshots, PERIODS[period], truncated), len(snapshots), truncated)


def _snapshots(runner: ToolRunner, root: Path, relative: str, commits: list[list[str]]) -> list[tuple[str, dict[str, int]]]:
    """``(date, {entry: weight})`` for each revision, oldest first; a revision that does not read as a baseline is skipped."""
    result: list[tuple[str, dict[str, int]]] = []
    for sha, day in commits:
        shown = runner.run(["git", "show", f"{sha}:{relative}"], root)
        if shown.returncode != 0:
            continue
        try:
            _, entries = parse_entries(json.loads(shown.stdout))
        except ValueError:
            continue
        weights: dict[str, int] = {}
        for entry in entries:
            weights[entry.ident] = weights.get(entry.ident, 0) + entry.weight
        result.append((day, weights))
    return result


def _periods(snapshots: list[tuple[str, dict[str, int]]], width: int, seeded: bool) -> list[Period]:
    """Fold per-revision changes into periods; a weight that rose counts as added, one that fell as removed.
    ``seeded``: the oldest revision read is where history was cut off, so it is the starting point, not an addition."""
    changes: dict[str, list[int]] = {}
    previous: dict[str, int] = dict(snapshots[0][1]) if seeded else {}
    for day, weights in snapshots:
        added = sum(max(0, weights[k] - previous.get(k, 0)) for k in weights)
        removed = sum(max(0, w - weights.get(k, 0)) for k, w in previous.items())
        bucket = changes.setdefault(day[:width], [0, 0, 0])
        bucket[0] += added
        bucket[1] += removed
        bucket[2] = sum(weights.values())
        previous = weights
    return [Period(label, a, r, total) for label, (a, r, total) in sorted(changes.items())]
