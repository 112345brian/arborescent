"""Opt-in save analysis for a project with an Arborescent policy.

``violations`` and ``notes`` hold only ``PARITY_RULES``, which match a fresh full scan of this project. A clean result
here does not mean ``arborescent check`` passes: no other rule is evaluated and no baseline is applied.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from arborescent.adapters import FsScanner, LocalFileSystem, ScanData
from arborescent.config import load_policy
from arborescent.domain import (
    EFFECT_RULES,
    INFORMATIONAL_RULES,
    Policy,
    Rule,
    Violation,
)
from arborescent.effect_import_rules import unlisted_category_findings
from arborescent.engine import discovery_findings, patch_findings
from arborescent.facts import SourceFacts, extract
from arborescent.rules import RuleContext, check_modules

# `PARITY_RULES` are the rules `tests/test_incremental_parity.py` proves equal to a full scan. Findings of every other rule are dropped.
_IMPORT_RULES = frozenset(
    {
        Rule.ARBOR_RING_MISMATCH,
        Rule.BAD_IMPORT,
        Rule.CIRCULAR_IMPORT,
        Rule.DEV_TOOL_IMPORT,
        Rule.DEV_TOOL_MISPLACED,
        Rule.DEV_TOOL_PRODUCT_IMPORT,
        Rule.EXTERNAL_IMPORT,
        Rule.FORBIDDEN_MODULE,
        Rule.INTERNAL_IMPORT,
        Rule.INVALID_ARBOR_ANNOTATION,
        Rule.NOISE_DIR_CODE,
        Rule.RESTRICTED_IMPORT,
        Rule.SCOPE_IMPORT,
        Rule.UNANNOTATED_MODULE,
        Rule.UNCLASSIFIED,
        Rule.UNDECLARED_ENTRY_DEPENDENCY,
        Rule.VENDOR_FOLDER,
        Rule.WILDCARD_RING,
    }
)
_EFFECT_PARITY_RULES = frozenset(EFFECT_RULES.values()) | {
    Rule.EFFECT_AT_IMPORT,
    Rule.EFFECT_SEAM_BYPASSED,
    Rule.EFFECT_UNLISTED_CATEGORY,
    Rule.EFFECT_UNSANCTIONED_IMPORT,
    Rule.EFFECT_VIA_IMPORT,
    Rule.OS_BRANCHING,
    Rule.SCOPE_EFFECT_LEAK,
    Rule.UNGUARDED_MODULE_STATE,
    Rule.UNGUARDED_MODULE_STATE_NOTE,
    Rule.UNREACHABLE_PATCH,
    Rule.UNRESOLVED_PATCH_TARGET,
}
PARITY_RULES: frozenset[str] = _IMPORT_RULES | _EFFECT_PARITY_RULES


@dataclass(frozen=True)
class Result:
    parsed: int
    # `PARITY_RULES` findings: those `check` fails on (before its baseline), and those it only reports.
    violations: tuple[Violation, ...]
    notes: tuple[Violation, ...]
    syntax_errors: tuple[str, ...]


@dataclass(frozen=True)
class _Parsed:
    """One file as the full scan reads it, or empty facts when CPython rejects it."""

    source: str
    facts: SourceFacts
    syntax_error: bool


def _parse(source: str, path: str) -> _Parsed:
    try:
        return _Parsed(source, extract(source, path), False)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _Parsed(source, SourceFacts((), (), None), True)


@dataclass(frozen=True)
class _Checked:
    """Everything ``_scan_findings`` reads, so a save that leaves it alone reuses its findings."""

    policy: Policy
    scan: ScanData


def _scan_findings(checked: _Checked) -> list[Violation]:
    """What the full scan's own rule functions find from the policy and the scan alone."""
    scan = checked.scan
    context = RuleContext(
        checked.policy,
        scan.arbor_index,
        scan.scope_by_module,
        scan.dev_tool_modules,
    )
    found = check_modules(
        checked.policy, scan.modules, scan.known, scan.entry_deps, context
    )
    found += discovery_findings(context, scan)
    found += unlisted_category_findings(context, scan.modules)
    return found


class Analysis:
    """Save-time analysis of the project whose policy file sits in ``root``."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._fs = LocalFileSystem()
        # The full scan's own discovery and module selection. No fact cache, so every refresh reads every source.
        self._scanner = FsScanner(extractor=self._extract)
        self._parsed: dict[str, _Parsed] = {}
        self._seen: dict[str, _Parsed] = {}  # the files this refresh's scan read
        # Paths extracted since the last completed refresh, so one that failed part way still counts them.
        self._fresh: set[str] = set()
        self._checked: _Checked | None = None
        self._found: list[Violation] = []

    def _extract(self, source: str, path: str) -> SourceFacts:
        parsed = self._parsed.get(path)
        if parsed is None or parsed.source != source:
            parsed = self._parsed[path] = _parse(source, path)
            self._fresh.add(path)
        self._seen[path] = parsed
        return parsed.facts

    def _broken(self, path: str) -> bool:
        return path in self._seen and self._seen[path].syntax_error

    def _scan(self, policy: Policy) -> ScanData:
        self._seen = {}
        scan = self._scanner.scan(policy, self._root)
        # A full scan leaves out a test file that does not parse, so its empty facts must not count as a test.
        tests = {
            path: facts
            for path, facts in scan.test_facts.items()
            if not self._broken(path)
        }
        return replace(scan, test_facts=tests)

    def _findings(self, checked: _Checked) -> list[Violation]:
        if checked != self._checked:
            self._found = _scan_findings(checked)
            self._checked = checked
        # Not cached: `unresolved-patch-target` reads files and folders that the scan does not describe.
        patched = patch_findings(checked.policy, self._root, self._fs, checked.scan)
        found = [v for v in self._found + patched if v.rule in PARITY_RULES]
        return sorted(found, key=lambda violation: (violation.key, violation.line))

    def refresh(self, policy: Policy) -> Result:
        # `mixed-purity` is not a parity rule, so do not spend a save on it.
        policy = replace(
            policy, mixed_purity=replace(policy.mixed_purity, enabled=False)
        )
        scan = self._scan(policy)
        found = self._findings(_Checked(policy, scan))
        parsed = sum(info.path in self._fresh for info in scan.modules.values())
        self._fresh.clear()
        return Result(
            parsed,
            violations=tuple(v for v in found if v.rule not in INFORMATIONAL_RULES),
            notes=tuple(v for v in found if v.rule in INFORMATIONAL_RULES),
            # Only discovered source files: a full scan skips an excluded or test file that does not parse.
            syntax_errors=tuple(
                sorted(path for path in scan.paths.values() if self._broken(path))
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    fs = LocalFileSystem()
    policy_path = args.policy.resolve()
    previous = None
    analysis = Analysis(policy_path.parent)
    try:
        while True:
            policy, _ = load_policy(fs, policy_path)
            try:
                result = analysis.refresh(policy)
            except FileNotFoundError:
                if not args.watch:
                    raise
                time.sleep(0.05)  # renamed between listing and reading
                continue
            # Compared as data: `Violation` equality leaves out hints and targets, which are shown.
            current = asdict(replace(result, parsed=0))
            if current != previous:
                sys.stdout.write(json.dumps(asdict(result)) + "\n")
                sys.stdout.flush()
                previous = current
            if not args.watch:
                return int(bool(result.violations or result.syntax_errors))
            time.sleep(0.05)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
