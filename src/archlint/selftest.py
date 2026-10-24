"""``selftest``: mutation-test the policy. For each rule, inject a forbidden construct into a temp COPY of the code
and require the checks to report it; a mutation that goes unreported is a rule that does not actually bite.

Ported from the throwaway mutation script behind DeepInterview's ``tests/test_architecture.py``.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .domain import Policy
from .engine import Services, run_own

__all__ = ["Mutation", "SelfTestReport", "selftest"]

_SKIP_COPY = shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__", "node_modules", ".archlint", "*.pyc")


@dataclass(frozen=True)
class Mutation:
    rule: str  # the rule id that must newly fire
    label: str
    file: str  # posix path relative to the root
    append: str  # text appended to the file ("" for an added file: see ``new_file``)
    new_file: bool = False


@dataclass
class SelfTestReport:
    killed: list[str]
    survived: list[str]
    skipped: list[str]

    @property
    def ok(self) -> bool:
        return not self.survived


def plan(policy: Policy, paths: dict[str, str], known: frozenset[str]) -> tuple[list[Mutation], list[str]]:
    """The mutations for this policy, plus the ones that cannot be built (and why)."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for ring in policy.rings:
        mine = sorted(m for m in known if policy.ring_of(m) == ring.name and not paths[m].endswith("__init__.py"))
        if not mine:
            skipped.append(f"ring {ring.name}: no module to mutate")
            continue
        host = mine[0]
        allowed = policy.allowed_rings(ring.name)
        target = next((m for m in sorted(known) if (r := policy.ring_of(m)) is not None and r not in allowed), None)
        for lazy in (False, True):
            how = "lazy" if lazy else "top-level"
            if target:
                code = f"import {target}" if not lazy else f"def _archlint_probe() -> None:\n    import {target}"
                out.append(Mutation("internal-import", f"{ring.name}: forbidden {how} internal import of {target}", paths[host], code))
            else:
                skipped.append(f"ring {ring.name}: allowed to import every ring, no forbidden internal import to inject")
            if "*" not in ring.external:
                code = "import archlint_probe_pkg" if not lazy else "def _archlint_probe2() -> None:\n    import archlint_probe_pkg"
                out.append(Mutation("external-import", f"{ring.name}: unlisted {how} external import", paths[host], code))
        if not ring.os_branching:
            out.append(Mutation("os-branching", f"{ring.name}: OS branching", paths[host], "import os\n_archlint_probe = os.name"))
    anchor = next(iter(sorted(known)), None)
    if anchor:
        base = paths[anchor].rsplit("/", 1)[0] if "/" in paths[anchor] else ""
        new = (base + "/" if base else "") + "archlint_probe_unclassified.py"
        out.append(Mutation("unclassified", "a new module matching no pattern", new, "", new_file=True))
    if policy.entry_scripts:
        entry_host = next((m for m in sorted(known) if policy.ring_of(m) and m not in policy.entry_scripts), None)
        if entry_host:
            out.append(Mutation("undeclared-entry-dependency", "a project module imports a package the scripts don't declare", paths[entry_host], "import archlint_probe_dist"))
    return out, sorted(set(skipped))


def selftest(policy: Policy, root: Path, services: Services) -> SelfTestReport:
    services = replace(services, scanner=services.scanner.uncached())  # mutated copies must not touch the real cache
    _, scan = run_own(policy, root, services, practices=False)
    mutations, skipped = plan(policy, dict(scan.paths), scan.known)
    report = SelfTestReport([], [], skipped)
    with tempfile.TemporaryDirectory(prefix="archlint-selftest-") as tmp:
        work = Path(tmp) / "copy"
        shutil.copytree(root, work, ignore=_SKIP_COPY)
        baseline = {v.key for v in run_own(policy, work, services, practices=False)[0]}
        for m in mutations:
            target = work / m.file
            original = target.read_bytes() if target.exists() and not m.new_file else None
            if m.new_file:
                target.write_text("X = 1\n", encoding="utf-8", newline="\n")
            else:
                target.write_bytes((original or b"") + b"\n" + m.append.encode() + b"\n")
            try:
                after = {v.key for v in run_own(policy, work, services, practices=False)[0] if v.rule == m.rule}
            finally:
                if m.new_file:
                    target.unlink()
                else:
                    target.write_bytes(original or b"")
            (report.killed if after - baseline else report.survived).append(f"[{m.rule}] {m.label}")
    return report
