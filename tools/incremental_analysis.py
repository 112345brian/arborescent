"""Opt-in save analysis for a project with an Arborescent policy.

``violations`` and ``notes`` hold only ``PARITY_RULES``, which match a fresh full scan of this project. ``call_paths``
is advisory evidence of its own (``effect-via-call``), which ``arborescent check`` has no equivalent of. A clean result
here does not mean ``arborescent check`` passes: no other rule is evaluated and no baseline is applied.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TypedDict, cast

from arborescent.adapters import FsScanner, LocalFileSystem, ScanData
from arborescent.config import load_policy
from arborescent.domain import (
    EFFECT_RULES,
    INFORMATIONAL_RULES,
    Policy,
    Rule,
    Violation,
)
from arborescent.effect_categories import effect_category_of
from arborescent.effect_import_rules import unlisted_category_findings
from arborescent.engine import discovery_findings, patch_findings
from arborescent.facts import SourceFacts, extract
from arborescent.rules import RuleContext, check_modules

# `PARITY_RULES` are the rules `test_parity.py` proves equal to a full scan. Findings of every other rule are dropped.
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
EFFECT_TARGETS = ("os.getenv",)


class Source(TypedDict):
    text: str
    is_package: bool


class ModuleEvidence(TypedDict):
    syntax_error: bool
    unresolved_calls: int


class Effect(TypedDict):
    function: str
    module: str
    target: str
    line: int
    chain: list[str]


class Evidence(TypedDict):
    parsed: int
    modules: dict[str, ModuleEvidence]
    effects: list[Effect]


class Worker:
    def __init__(self, binary: Path) -> None:
        self._binary = binary.resolve()

    def __enter__(self) -> Worker:
        self._process = subprocess.Popen(
            [str(self._binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        self._process.__exit__(None, None, None)

    def update(self, upsert: dict[str, Source], remove: list[str]) -> Evidence:
        request = {"upsert": upsert, "remove": remove, "effect_targets": EFFECT_TARGETS}
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("incremental worker pipes are unavailable")
        self._process.stdin.write(json.dumps(request) + "\n")
        self._process.stdin.flush()
        response = self._process.stdout.readline()
        if not response:
            raise RuntimeError("incremental worker exited without returning evidence")
        return cast("Evidence", json.loads(response))


@dataclass(frozen=True)
class CallPath(Violation):
    """``effect-via-call``: a function that reaches a forbidden effect through calls.

    Its baseline identity (``key``) names only the rule, module, ring, function and effect category, so moved lines and
    a different route to the same effect keep it. The route is evidence, in ``line``, ``target`` and ``chain``."""

    chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    parsed: int
    # `PARITY_RULES` findings: those `check` fails on (before its baseline), and those it only reports.
    violations: tuple[Violation, ...]
    notes: tuple[Violation, ...]
    # Advisory: no full-scan equivalent, never a failure.
    call_paths: tuple[CallPath, ...]
    # Where `call_paths` may miss a path: module -> calls not followed, for modules whose ring forbids a traced effect.
    unresolved_calls: dict[str, int]
    syntax_errors: tuple[str, ...]
    # Valid Python the worker's grammar rejects: `call_paths` evidence from these files is missing.
    incomplete: tuple[str, ...]


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


def _forbidden_in(context: RuleContext, name: str) -> tuple[str, frozenset[str]]:
    """The module's ring and the effects it forbids; nothing for developer tooling and unclassified modules."""
    if name in context.tool_modules:  # developer tooling has no ring
        return "", frozenset()
    owner = context.policy_for(name)
    ring = context.resolve_ring(name)
    if owner is None or ring is None:
        return "", frozenset()
    return ring, owner.forbidden_effects(ring)


def _call_paths(evidence: Evidence, context: RuleContext) -> list[CallPath]:
    findings: list[CallPath] = []
    for effect in evidence["effects"]:
        name = effect["module"]
        ring, forbidden = _forbidden_in(context, name)
        category = effect_category_of(effect["target"])
        if category in forbidden:
            findings.append(
                CallPath(
                    "effect-via-call",
                    name,
                    f"[{ring}] {effect['function']} reaches forbidden {category} effect",
                    line=effect["line"],
                    target=effect["target"],
                    chain=tuple(effect["chain"]),
                )
            )
    return sorted(findings, key=lambda found: (found.key, found.line, found.chain))


def _unresolved_calls(evidence: Evidence, context: RuleContext) -> dict[str, int]:
    traced = {effect_category_of(target) for target in EFFECT_TARGETS}
    return {
        name: facts["unresolved_calls"]
        for name, facts in sorted(evidence["modules"].items())
        if facts["unresolved_calls"] and traced & _forbidden_in(context, name)[1]
    }


@dataclass(frozen=True)
class _Checked:
    """Everything ``_scan_findings`` reads, so a save that leaves it alone reuses its findings."""

    policy: Policy
    scan: ScanData


def _context(checked: _Checked) -> RuleContext:
    scan = checked.scan
    return RuleContext(
        checked.policy,
        scan.arbor_index,
        scan.scope_by_module,
        scan.dev_tool_modules,
    )


def _scan_findings(checked: _Checked) -> list[Violation]:
    """What the full scan's own rule functions find from the policy and the scan alone."""
    context = _context(checked)
    scan = checked.scan
    found = check_modules(
        checked.policy, scan.modules, scan.known, scan.entry_deps, context
    )
    found += discovery_findings(context, scan)
    found += unlisted_category_findings(context, scan.modules)
    return found


class Analysis:
    """Save-time analysis of the project whose policy file sits in ``root``."""

    def __init__(self, worker: Worker, root: Path) -> None:
        self._worker = worker
        self._root = root.resolve()
        self._fs = LocalFileSystem()
        # The full scan's own discovery and module selection. No fact cache, so every refresh reads every source.
        self._scanner = FsScanner(extractor=self._extract)
        self._parsed: dict[str, _Parsed] = {}
        self._seen: dict[str, _Parsed] = {}  # the files this refresh's scan read
        self._sources: dict[str, Source] = {}
        self._evidence: Evidence | None = None
        self._checked: _Checked | None = None
        self._found: list[Violation] = []

    def _extract(self, source: str, path: str) -> SourceFacts:
        parsed = self._parsed.get(path)
        if parsed is None or parsed.source != source:
            parsed = self._parsed[path] = _parse(source, path)
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

    def _update(self, scan: ScanData) -> tuple[int, Evidence]:
        sources: dict[str, Source] = {}
        for name, info in scan.modules.items():
            parsed = self._seen[info.path]
            # The worker never sees what CPython rejects, so it keeps no calls from it. Python reads a lone carriage
            # return as a line end and tree-sitter does not.
            text = "" if parsed.syntax_error else parsed.source
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            sources[name] = {"text": text, "is_package": info.is_package}
        upsert = {
            name: source
            for name, source in sources.items()
            if self._sources.get(name) != source
        }
        remove = sorted(self._sources.keys() - sources.keys())
        if self._evidence is not None and not upsert and not remove:
            return 0, self._evidence
        self._evidence = self._worker.update(upsert, remove)
        self._sources = sources
        return self._evidence["parsed"], self._evidence

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
        parsed, evidence = self._update(scan)
        checked = _Checked(policy, scan)
        found = self._findings(checked)
        context = _context(checked)
        return Result(
            parsed,
            violations=tuple(v for v in found if v.rule not in INFORMATIONAL_RULES),
            notes=tuple(v for v in found if v.rule in INFORMATIONAL_RULES),
            call_paths=tuple(_call_paths(evidence, context)),
            unresolved_calls=_unresolved_calls(evidence, context),
            # Only discovered source files: a full scan skips an excluded or test file that does not parse.
            syntax_errors=tuple(
                sorted(path for path in scan.paths.values() if self._broken(path))
            ),
            # The worker is never sent what CPython rejects, so these are valid Python its grammar cannot parse.
            incomplete=tuple(
                sorted(
                    info.path
                    for name, info in scan.modules.items()
                    if evidence["modules"][name]["syntax_error"]
                )
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    fs = LocalFileSystem()
    policy_path = args.policy.resolve()
    previous = None
    with Worker(args.worker) as worker:
        analysis = Analysis(worker, policy_path.parent)
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
