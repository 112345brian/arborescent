"""The gates ``check`` and ``baseline`` add on top of ``run_check``: baseline growth and admission since a git ref, and
the engine findings ``baseline --prune`` needs so it does not drop entries that are still valid.

Split out of ``cli``: each reads through the injected ports and returns a value; printing stays in the command.
"""

from __future__ import annotations

from pathlib import Path

from . import baseline as ratchet_mod
from .admitted_against import admission_problems
from .domain import Policy, Violation
from .engine import EngineRun, Services, run_engine, run_own

__all__ = ["admission_check", "growth_check", "stale_engine_findings"]


def stale_engine_findings(svc: Services, root: Path, policy: Policy) -> list[Violation]:
    """``--prune`` judges every baseline entry, so each engine that owns a baselined rule family must be re-run, or its
    still-valid entries would look stale and be dropped."""
    rules = {key.split("|", 1)[0] for key in ratchet_mod.load(svc.fs, root / policy.baseline)}
    names = {item.name for item in (*svc.engines, *svc.checkers)} - {"ruff"}  # `_baseline` already re-ran ruff for its entries
    extra: list[Violation] = []
    for name in sorted(names):
        if any(r == name or r.startswith(f"{name}-") for r in rules):
            extra += run_engine(svc, name, root).violations
    return extra


def _json_at_ref(ref: str, path: str, root: Path, svc: Services) -> dict[str, object]:
    """The JSON object stored at ``path`` in git ``ref``; ValueError when it cannot be read."""
    git = svc.runner.run(["git", "show", f"{ref}:./{path}"], root)  # `./`: relative to `root`, which may be below the repository root
    if git.returncode != 0:
        raise ValueError(f"could not read {path} at {ref}: {git.stderr.strip() or git.stdout.strip()}")
    with svc.fs.temporary_directory("arborescent-growth-") as temp:
        copy = temp / "ratchet.json"
        svc.fs.write_text(copy, git.stdout)
        data = svc.fs.read_json(copy)
    if not isinstance(data, dict):
        raise ValueError(f"{path} at {ref} is not a JSON object")
    return {str(key): value for key, value in data.items()}


def growth_check(ref: str, policy: Policy, root: Path, svc: Services) -> EngineRun:
    started = svc.clock.now()
    lines: list[str] = []
    try:
        current = ratchet_mod.document(svc.fs, root / policy.baseline)
        if not ratchet_mod.growth_consistent(current, _json_at_ref(ref, policy.baseline, root, svc)):
            lines.append(f"baseline changes since {ref} are not fully accounted for by new growth log entries with a reason and approved-by")
        for name in policy.ratchet_files:
            current_values = svc.fs.read_json(root / name)
            if not isinstance(current_values, dict):
                raise ValueError(f"{name} is not a JSON object")
            lines.extend(f"{name}: {p}" for p in ratchet_mod.values_growth_problems(current_values, _json_at_ref(ref, name, root, svc)))
    except (ValueError, TypeError, OSError) as exc:
        lines.append(f"could not verify ratchet growth: {exc}")
    status = "fail" if lines else "ok"
    return EngineRun("baseline growth log", status, svc.clock.now() - started, lines or [f"growth logs match ratchet changes since {ref}"])


def admission_check(ref: str, policy: Policy, root: Path, svc: Services) -> EngineRun:
    """#163: every module new since ``ref`` must be validly admitted; an unreadable ref or a shallow clone fails, never passes."""
    started = svc.clock.now()
    try:
        _, scan = run_own(policy, root, svc, practices=False)  # a full scan: admission needs every module's ring and importers
        lines = admission_problems(ref, policy, root, scan, svc.runner)
    except (ValueError, OSError) as exc:
        lines = [f"could not verify admission: {exc}"]
    status = "fail" if lines else "ok"
    return EngineRun("admission", status, svc.clock.now() - started, lines or [f"every module new since {ref} was validly admitted"])
