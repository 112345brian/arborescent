"""``check --admitted-against REF`` (#163, stage 3 of the admission design): a module new since REF must have been validly admitted.

New modules are the ``.py`` files ``git diff -M --diff-filter=A`` reports added between the merge base of REF and HEAD, read
through the ``ToolRunner`` port. Admission is recomputed at HEAD with ``admit_check`` (the same strict test as ``arborescent
admit``), so a hand-written ``__arbor__`` is accepted only if it passes. A module fails when it has no explicit ``__arbor__`` ring
(a ``classify`` glob does not count), when admission refuses it, or (``admission-looser-than-needed``) when a tighter ring fits
and no ``ring_reason`` is recorded. Existing modules, renames and deletions are never judged. Anything git cannot answer raises
``ValueError``: an unreadable ref or a shallow clone fails the check, it never passes it.
"""

from __future__ import annotations

from pathlib import Path

from .classify_audit import admit_check
from .domain import Policy
from .ports import ScanResult, ToolRunner
from .rules import RuleContext

__all__ = ["LOOSER_THAN_NEEDED", "REFUSED", "admission_problems", "new_module_paths"]

REFUSED = "admission-refused"
LOOSER_THAN_NEEDED = "admission-looser-than-needed"
_DEEPEN = "fetch more history (`git fetch --deepen=N`, or `fetch-depth: 0` in CI) so REF and HEAD share a commit"


def new_module_paths(runner: ToolRunner, root: Path, ref: str) -> list[str]:
    """Repository-relative (to ``root``) ``/``-separated paths of the Python files added since ``ref``; ValueError when git cannot say."""
    if not ref.strip() or ref.startswith("-"):
        raise ValueError(f"{ref!r} is not a usable ref")
    found = runner.run(["git", "merge-base", ref, "HEAD"], root)
    base = found.stdout.strip()
    if found.returncode != 0 or not base:
        detail = (found.stderr.strip() or found.stdout.strip() or "no output").splitlines()[0]
        raise ValueError(f"could not find a merge base of {ref} and HEAD ({detail}); is the ref spelled right? Otherwise {_DEEPEN}")
    listed = runner.run(
        ["git", "-c", "core.quotepath=off", "diff", "--name-status", "-M", "--diff-filter=A", "--relative", base, "HEAD"], root
    )
    if listed.returncode != 0:
        raise ValueError(f"could not list the files added since {ref}: {listed.stderr.strip() or listed.stdout.strip() or 'git failed'}")
    paths: list[str] = []
    for line in listed.stdout.splitlines():
        status, _, path = line.partition("\t")
        if status.startswith("A") and path.replace("\\", "/").endswith(".py"):
            paths.append(path.replace("\\", "/"))
    return paths


def admission_problems(ref: str, policy: Policy, root: Path, scan: ScanResult, runner: ToolRunner) -> list[str]:
    """One line per failing new module, each starting with its rule id; empty when every new module was validly admitted.

    ``scan`` must come from a full (not ``--changed``) scan, so the module's importers and rings are all known.
    """
    added = new_module_paths(runner, root, ref)
    by_path = {path.replace("\\", "/"): name for name, path in scan.paths.items()}
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    known = sorted(scan.known)
    lines: list[str] = []
    for path in added:
        name = by_path.get(path)
        if name is None:
            continue  # not a scanned module: excluded, outside every source root, or a test
        ring, error = scan.arbor_index.get(name, (None, None))
        info = scan.modules.get(name)
        if error is not None:
            lines.append(f"{REFUSED}: {name}: invalid __arbor__ annotation: {error}")
        elif ring is None:
            lines.append(f"{REFUSED}: {name}: new since {ref} with no explicit `__arbor__` ring (a `classify` match does not count as admitted); run `arborescent admit {name} --ring R`")
        elif info is None:
            lines.append(f"{REFUSED}: {name} [{ring}]: could not be scanned")
        else:
            outcome = admit_check(context, name, info, ring, known)
            for refusal in outcome.refusals:
                rule = LOOSER_THAN_NEEDED if outcome.tighter is not None and f"ring {outcome.tighter!r} also fits" in refusal else REFUSED
                lines.append(f"{rule}: {name} [{ring}]: {refusal}")
    return lines
