"""Opt-in save analysis for a project with an Arborescent policy.

It reports only ``PARITY_RULES``, which match a fresh full scan, plus its own ``effect-via-call``. A clean result here
does not mean ``arborescent check`` passes: no other rule is evaluated and no baseline is applied.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TypedDict, cast

from arborescent.adapters import FsScanner, LocalFileSystem, ScanData
from arborescent.config import load_policy
from arborescent.declaration_facts import arbor_annotation, script_deps
from arborescent.domain import Policy, Rule, Violation
from arborescent.effect_categories import effect_category_of
from arborescent.engine import discovery_findings
from arborescent.facts import SourceFacts
from arborescent.import_syntax import raw_imports
from arborescent.rules import RuleContext, check_modules

# The rules decided by file discovery, module names, imports, `__arbor__` and PEP 723 metadata alone. Every other rule
# needs facts the worker does not supply, so its findings are dropped rather than reported from partial facts.
PARITY_RULES: frozenset[str] = frozenset(
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
EFFECT_TARGETS = ("os.getenv",)


class Source(TypedDict):
    text: str
    is_package: bool


class ModuleEvidence(TypedDict):
    syntax_error: bool


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
class Finding(Violation):
    chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    parsed: int
    violations: tuple[Finding, ...]
    syntax_errors: tuple[str, ...]
    # Valid Python the worker's grammar rejects: call evidence from these files is missing.
    incomplete: tuple[str, ...]


@dataclass(frozen=True)
class _Headline:
    """What CPython decides for a file: whether it parses, its imports, and the metadata that selects modules and rings."""

    source: str
    facts: SourceFacts
    syntax_error: bool


def _headline(source: str) -> _Headline:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _Headline(source, SourceFacts((), (), None), True)
    ring, error, role, reason = arbor_annotation(tree)
    facts = SourceFacts(
        raw_imports(tree),
        (),
        script_deps(source),
        arbor_ring=ring,
        arbor_error=error,
        arbor_role=role,
        arbor_reason=reason,
    )
    return _Headline(source, facts, False)


def _effect_findings(evidence: Evidence, context: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    for effect in evidence["effects"]:
        name = effect["module"]
        if name in context.tool_modules:  # developer tooling has no ring
            continue
        owner = context.policy_for(name)
        ring = context.resolve_ring(name)
        category = effect_category_of(effect["target"])
        if (
            owner is not None
            and ring is not None
            and category in owner.forbidden_effects(ring)
        ):
            findings.append(
                Finding(
                    "effect-via-call",
                    name,
                    f"[{ring}] {effect['function']} reaches forbidden {category} effect",
                    line=effect["line"],
                    target=effect["target"],
                    chain=tuple(effect["chain"]),
                )
            )
    return findings


@dataclass(frozen=True)
class _Checked:
    """Everything the parity rules read, so a save that leaves it alone reuses their findings."""

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


def _parity_findings(checked: _Checked) -> list[Finding]:
    context = _context(checked)
    scan = checked.scan
    found = check_modules(
        checked.policy, scan.modules, scan.known, scan.entry_deps, context
    )
    found += discovery_findings(context, scan)
    return [
        Finding(
            v.rule,
            v.subject,
            v.detail,
            line=v.line,
            target=v.target,
            hint=v.hint,
            root_cause=v.root_cause,
        )
        for v in found
        if v.rule in PARITY_RULES
    ]


class Analysis:
    """Save-time analysis of the project whose policy file sits in ``root``."""

    def __init__(self, worker: Worker, root: Path) -> None:
        self._worker = worker
        self._root = root.resolve()
        # The full scan's own discovery and module selection. No fact cache, so every refresh reads every source.
        self._scanner = FsScanner(extractor=self._extract)
        self._parsed: dict[str, _Headline] = {}
        self._seen: dict[str, _Headline] = {}  # the files this refresh's scan read
        self._sources: dict[str, Source] = {}
        self._evidence: Evidence | None = None
        self._scan: ScanData | None = None
        self._policy: Policy | None = None
        self._result: Result | None = None
        self._checked: _Checked | None = None
        self._parity: list[Finding] = []

    def _extract(self, source: str, path: str) -> SourceFacts:
        headline = self._parsed.get(path)
        if headline is None or headline.source != source:
            headline = self._parsed[path] = _headline(source)
        self._seen[path] = headline
        return headline.facts

    def refresh(self, policy: Policy) -> Result:
        self._seen = {}
        scan = self._scanner.scan(policy, self._root)
        sources: dict[str, Source] = {}
        for name, info in scan.modules.items():
            headline = self._seen[info.path]
            # The worker never sees what CPython rejects, so it keeps no calls from it. Python reads a lone carriage
            # return as a line end and tree-sitter does not.
            text = "" if headline.syntax_error else headline.source
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            sources[name] = {"text": text, "is_package": info.is_package}
        upsert = {
            name: source
            for name, source in sources.items()
            if self._sources.get(name) != source
        }
        remove = sorted(self._sources.keys() - sources.keys())
        # Only discovered source files: a full scan skips an excluded or test file that does not parse. A broken file
        # outside enforcement changes neither the scan nor the worker's sources, so the cache must compare this too.
        broken = tuple(
            sorted(
                path
                for path in scan.paths.values()
                if path in self._seen and self._seen[path].syntax_error
            )
        )
        if self._evidence is None or upsert or remove:
            self._evidence = self._worker.update(upsert, remove)
            self._sources = sources
            parsed = self._evidence["parsed"]
        elif self._result is not None and (policy, scan, broken) == (
            self._policy,
            self._scan,
            self._result.syntax_errors,
        ):
            return replace(self._result, parsed=0)
        else:
            parsed = 0
        findings, incomplete = self._evaluate(self._evidence, policy, scan)
        result = Result(parsed, findings, broken, incomplete)
        self._result = result
        self._policy = policy
        self._scan = scan
        return result

    def _evaluate(
        self, evidence: Evidence, policy: Policy, scan: ScanData
    ) -> tuple[tuple[Finding, ...], tuple[str, ...]]:
        """The findings, and the paths whose calls are missing."""
        # The worker is never sent what CPython rejects, so these are valid Python its grammar cannot parse.
        incomplete = tuple(
            sorted(
                info.path
                for name, info in scan.modules.items()
                if evidence["modules"][name]["syntax_error"]
            )
        )
        checked = _Checked(policy, scan)
        if checked != self._checked:
            self._parity = _parity_findings(checked)
            self._checked = checked
        findings = self._parity + _effect_findings(evidence, _context(checked))
        ordered = sorted(
            findings, key=lambda found: (found.key, found.line, found.chain)
        )
        return tuple(ordered), incomplete


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
                current = (result.violations, result.syntax_errors, result.incomplete)
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
