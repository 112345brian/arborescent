"""Opt-in save analysis for a Python source root with an Arborescent policy."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TypedDict, cast

from arborescent.config import load_policy
from arborescent.domain import Policy, Rule, Violation
from arborescent.effect_categories import effect_category_of
from arborescent.facts import ModuleInfo, SourceFacts
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.import_facts import RawImport
from arborescent.path_exclusion import excluded_by_policy
from arborescent.rules import RuleContext, check_modules

EFFECT_TARGETS = ("os.getenv",)


class Source(TypedDict):
    text: str
    is_package: bool


class Import(TypedDict):
    level: int
    module: str
    names: list[str]
    line: int
    lazy: bool
    type_only: bool


class ModuleEvidence(TypedDict):
    imports: list[Import]
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


def _evaluate(
    evidence: Evidence,
    policy: Policy,
    paths: dict[str, str],
    sources: dict[str, Source],
    parsed: int,
) -> Result:
    modules: dict[str, ModuleInfo] = {}
    scopes: dict[str, str] = {}
    tool_modules: set[str] = set()
    syntax_errors: list[str] = []
    for name, facts in evidence["modules"].items():
        path = paths[name]
        scope = policy.scope_for_path(path)
        if scope is not None:
            scopes[name] = scope.name
        if policy.is_dev_tool_path(path):
            tool_modules.add(name)
        if facts["syntax_error"]:
            syntax_errors.append(path)
        imports = tuple(
            RawImport(
                imp["level"],
                imp["module"],
                tuple(imp["names"]),
                imp["line"],
                imp["lazy"],
                imp["type_only"],
            )
            for imp in facts["imports"]
        )
        modules[name] = ModuleInfo(
            name, path, sources[name]["is_package"], SourceFacts(imports, (), None)
        )

    context = RuleContext(policy, {}, scopes, frozenset(tool_modules))
    imports = check_modules(policy, modules, modules.keys(), {}, context)
    findings = [
        Finding(v.rule, v.subject, v.detail, line=v.line, target=v.target, hint=v.hint)
        for v in imports
        if v.rule
        in {
            Rule.INTERNAL_IMPORT,
            Rule.SCOPE_IMPORT,
        }  # Only the import rules exercised by the prototype.
    ]
    for effect in evidence["effects"]:
        name = effect["module"]
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
    return Result(
        parsed,
        tuple(
            sorted(
                findings, key=lambda finding: (finding.key, finding.line, finding.chain)
            )
        ),
        tuple(sorted(syntax_errors)),
    )


class Analysis:
    def __init__(
        self, worker: Worker, source_root: Path, policy_root: Path | None = None
    ) -> None:
        self._worker = worker
        self._source_root = source_root.resolve()
        self._policy_root = policy_root.resolve() if policy_root else self._source_root
        self._sources: dict[str, Source] = {}
        self._evidence: Evidence | None = None
        self._policy: Policy | None = None
        self._result: Result | None = None

    def refresh(self, policy: Policy) -> Result:
        paths: dict[str, str] = {}
        sources: dict[str, Source] = {}
        for path in sorted(self._source_root.rglob("*.py")):
            parts = path.relative_to(self._source_root).with_suffix("").parts
            is_package = parts[-1] == "__init__"
            segments = parts[:-1] if is_package else parts
            if not segments or not all(segment.isidentifier() for segment in segments):
                continue
            relative = path.relative_to(self._policy_root).as_posix()
            if excluded_by_policy(relative, policy.exclude):
                continue
            name = ".".join(segments)
            if name in paths:
                raise ValueError(f"multiple files provide module {name!r}")
            try:
                text = path.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                continue  # A rename between enumeration and read is picked up on the next refresh.
            paths[name] = relative
            sources[name] = {"text": text, "is_package": is_package}
        upsert = {
            name: source
            for name, source in sources.items()
            if self._sources.get(name) != source
        }
        remove = sorted(self._sources.keys() - sources.keys())
        if self._evidence is None or upsert or remove:
            self._evidence = self._worker.update(upsert, remove)
            self._sources = sources
            parsed = self._evidence["parsed"]
        elif self._result is not None and policy == self._policy:
            return replace(self._result, parsed=0)
        else:
            parsed = 0
        result = _evaluate(self._evidence, policy, paths, sources, parsed)
        self._result = result
        self._policy = policy
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    fs = LocalFileSystem()
    policy_path = args.policy.resolve()
    previous = None
    with Worker(args.worker) as worker:
        analysis = Analysis(worker, args.source_root, policy_path.parent)
        try:
            while True:
                policy, _ = load_policy(fs, policy_path)
                result = analysis.refresh(policy)
                current = (result.violations, result.syntax_errors)
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
