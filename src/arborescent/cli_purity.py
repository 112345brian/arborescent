"""``arborescent purity``: every mixed module, ranked by blast radius, with what to split out of it (#173)."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .domain import Policy, in_unit
from .engine import Services
from .import_cycles import import_edges
from .purity import (
    Followed,
    ModulePurity,
    assess_in_ring,
    by_ring,
    explain,
    is_allowed,
    summarize,
)
from .rule_context import RuleContext

_HEADER = "mixed modules, most imported first (a module's importers are the ones that inherit its effects):"


@dataclass(frozen=True)
class Listing:
    """What ``purity`` prints, never what it measures: ``importers`` always counts the whole scan (#217).

    ``paths`` (files or folders, relative to ``cwd``, else to the root) and ``modules`` (names or globs, submodules included)
    are one union; with neither, every mixed module is listed. ``top`` trims what is left."""

    top: int | None = None
    paths: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    cwd: Path | None = None


def _under(svc: Services, root: Path, base: Path, text: str) -> str | None:
    """``text`` as a posix path relative to the root; ``None`` when it is blank, does not exist or is outside the root."""
    if not text.strip():
        return None  # `Path("")` is the working directory: it would silently select everything below it
    try:
        return svc.fs.resolve(base / text).relative_to(svc.fs.resolve(root)).as_posix()
    except (OSError, ValueError):
        return None


def _selected(listing: Listing, root: Path, svc: Services, paths: Mapping[str, str]) -> set[str] | None:
    """The scanned modules (``paths``: name -> file) the listing names. ``None`` after printing why: a typo must not read as a clean result.

    An existing file or folder with no scanned module in it (a changed-files list holds docs and excluded tests) is only noted."""
    selected: set[str] = set()
    errors: list[str] = []
    for pattern in listing.modules:
        found = {name for name in paths if in_unit(name, pattern)}
        selected |= found
        if not found:
            errors.append(f"--module {pattern!r} matches no scanned module")
    for text in listing.paths:
        rel = _under(svc, root, listing.cwd or root, text)
        if rel is None:
            errors.append(f"--path {text!r} is not an existing file or folder under {root}")
            continue
        found = {name for name, path in paths.items() if rel in (".", path) or path.startswith(rel + "/")}
        selected |= found
        if not found:
            print(f"note: --path {text!r} holds no scanned module", file=sys.stderr)
    for error in errors:
        print(error, file=sys.stderr)
    return None if errors else selected


def ranked(policy: Policy, root: Path, svc: Services, listing: Listing) -> list[tuple[ModulePurity, str, bool]] | None:
    """(report, ring, allowed) for each mixed module under a ring: importers descending, then more pure units, then name.

    A scoped ``listing`` drops rows after everything is measured; ``None`` when it names something that does not exist."""
    scan = svc.scanner.scan(policy, root)
    selected: set[str] | None = None
    if listing.paths or listing.modules:
        selected = _selected(listing, root, svc, {name: info.path for name, info in scan.modules.items()})
        if selected is None:
            return None
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    rings = {name: ring for name in scan.modules if name not in context.tool_modules and (ring := context.resolve_ring(name)) is not None}
    edges = import_edges({name: scan.modules[name] for name in rings}, scan.known, context.scope_name)
    importers: dict[str, set[str]] = {}
    for edge in edges:
        importers.setdefault(edge.target, set()).add(edge.source)
    rows: list[tuple[ModulePurity, str, bool]] = []
    followed = Followed(scan.modules)
    for name, ring in rings.items():
        local = context.policy_for(name)
        report = assess_in_ring(scan.modules[name], local, policy.mixed_purity, ring, followed) if local is not None else None
        if local is not None and report is not None and (selected is None or name in selected):
            rows.append((replace(report, importers=len(importers.get(name, ()))), ring, is_allowed(policy.mixed_purity, name)))
    return sorted(rows, key=lambda row: (-row[0].importers, -len(row[0].pure), row[0].module))


def run_explain(policy: Policy, root: Path, svc: Services, module: str) -> int:
    """Per definition of one module: the call that made it effectful, or ``no effect found``. Works on a module that is not mixed."""
    scan = svc.scanner.scan(policy, root)
    info = scan.modules.get(module)
    if info is None:
        print(f"no scanned module named {module!r}", file=sys.stderr)
        return 2
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    local = context.policy_for(module)
    if local is None:
        print(f"{module} is not covered by a policy scope", file=sys.stderr)
        return 2
    print(f"{module} ({info.path}):")
    lines = explain(info, local, policy.mixed_purity, Followed(scan.modules))
    for line in lines or ["no top-level functions or classes"]:
        print(f"  {line}")
    return 0


def run_purity(policy: Policy, root: Path, svc: Services, as_json: bool, listing: Listing | None = None) -> int:
    """Report only: the ``mixed-purity`` rule under ``check`` is what fails a run. ``listing`` trims the output, never the findings."""
    listing = listing or Listing()
    top = listing.top
    if top is not None and top < 1:
        print(f"--top must be a positive number, got {top}", file=sys.stderr)
        return 2
    everything = ranked(policy, root, svc, listing)
    if everything is None:
        return 2
    where = " in the selected scope" if listing.paths or listing.modules else ""
    rows = everything[:top]
    if as_json:
        print(json.dumps([{**asdict(report), "ring": ring, "allowed": allowed} for report, ring, allowed in rows], indent=2))
        return 0
    if not rows:
        print(f"no mixed modules{where}")
        return 0
    totals = {ring: sum(1 for row in everything if row[1] == ring) for _, ring, _ in everything}
    print(_HEADER)
    for ring, group in by_ring(rows):
        print(f"[{ring}] {totals[ring]} mixed module(s)" + (f", showing {len(group)}" if len(group) != totals[ring] else ""))
        for report, _, allowed in group:
            print(f"  {report.module}:{report.line}{' (allowed)' if allowed else ''}{' (weak)' if report.weak else ''}{summarize(report)}")
    count = f"{len(everything)}" if len(rows) == len(everything) else f"showing {len(rows)} of {len(everything)}"
    print(f"{count} mixed module(s){where}")
    return 0
