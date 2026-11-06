"""Compile the one policy into the committed configs of the engines.

The import graph's rules are compiled once into neutral contracts (``architecture_contracts``) and each contract renderer
(import-linter, tach, or any engine the caller supplies) writes its own format; the type checkers render their own too. Pure:
``generate`` returns ``{relative path: text}``; ``check`` diffs that against the committed files and ``arborescent generate``
writes them. Nothing here is hand-edited, so the tools cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .architecture_contracts import compile_contracts
from .domain import NOISE_DIRS, Policy
from .generated_config import HEADER, toml_list, toml_quote
from .ports import ContractRenderer, PackageBoundaries, ScanResult, TypeChecker
from .rules import RuleContext

__all__ = ["GraphFacts", "generate", "generate_for_scan"]

_RUFF_SELECT = (
    "PLC0415", "BLE001", "E722", "C901", "PLR0912", "PLR0913", "PLR0915", "TID252", "PLW1514",
    "A", "ANN", "ASYNC", "C4", "DTZ", "I", "PERF", "PIE", "PTH", "RET", "RUF", "S", "SIM", "UP",
    "YTT", "T20", "F", "B",
)
_RUFF_IGNORE = frozenset({"PIE807"})
# `[tool.arborescent.ruff]` key -> (Ruff table, Ruff key). The pylint keys share one table.
_RUFF_LIMITS = {
    "max-complexity": ("mccabe", "max-complexity"),
    "max-args": ("pylint", "max-args"),
    "max-branches": ("pylint", "max-branches"),
    "max-returns": ("pylint", "max-returns"),
    "max-statements": ("pylint", "max-statements"),
}


def _ruff_limit_tables(limits: Mapping[str, int]) -> list[str]:
    """The ``[lint.mccabe]`` / ``[lint.pylint]`` tables for the limits that are set, each table once."""
    tables: dict[str, list[str]] = {}
    for key, (section, output_key) in _RUFF_LIMITS.items():
        if key in limits:
            tables.setdefault(section, []).append(f"{output_key} = {limits[key]}")
    return [line for section, rows in tables.items() for line in ("", f"[lint.{section}]", *rows)]


def _ruff_ignores(
    policy: Policy, names: Sequence[str], arbor_index: Mapping[str, tuple[str | None, str | None]] = {},
    scope_by_module: Mapping[str, str] = {},
) -> dict[str, set[str]]:
    ignores: dict[str, set[str]] = {}
    for pattern in ("**/tests/**/*.py", "**/test_*.py", "**/*_test.py"):
        ignores.setdefault(pattern, set()).update({"ANN", "S"})  # tests have different annotation/security idioms
    for pattern in ("**/pyproject.toml", "**/ruff.toml"):
        ignores.setdefault(pattern, set()).add("RUF201")  # generated configs intentionally use stable rule codes
    for module in names:
        if policy.scopes:
            scope_name = scope_by_module.get(module)
            if scope_name is None or RuleContext(policy, arbor_index, scope_by_module).resolve_ring(module) != policy.scope(scope_name).policy.rings[-1].name:
                continue
        elif policy.resolve_ring(arbor_index, module) != policy.rings[-1].name:
            continue
        base = module.replace(".", "/")
        for pattern in (f"**/{base}.py", f"**/{base}/**.py"):
            ignores.setdefault(pattern, set()).add("T20")
    overrides = policy.ruff_overrides
    for pattern, codes in overrides.get("per-file-ignores", {}).items():
        ignores.setdefault(pattern, set()).update(codes)
    return ignores


def ruff(
    policy: Policy, names: Sequence[str], arbor_index: Mapping[str, tuple[str | None, str | None]] = {},
    scope_by_module: Mapping[str, str] = {},
) -> str:
    """Render the shared Ruff rules and the small ring-specific exception for composition printing."""
    ignores = _ruff_ignores(policy, names, arbor_index, scope_by_module)
    overrides = policy.ruff_overrides
    vendor = ("vendor", "vendored", "third_party", "site-packages")
    excludes = sorted({
        pattern
        for name in (*NOISE_DIRS, *vendor)
        for pattern in (f"**/{name}", f"**/{name}/**")
    })
    lines = [
        HEADER.rstrip(),
        f'target-version = "py{policy.python.replace(".", "")}"',
        "force-exclude = true",
        f"exclude = {toml_list(excludes)}",
        "",
        "[lint]",
        "preview = true",
        f"select = {toml_list(list(dict.fromkeys([*_RUFF_SELECT, *policy.preset.ruff_select, *overrides.get('extend-select', [])])))}",
    ]
    ignored = {*_RUFF_IGNORE, *overrides.get("ignore", [])}
    if ignored:
        lines.append(f"ignore = {toml_list(sorted(ignored))}")
    if ignores:
        lines += ["", "[lint.per-file-ignores]"]
        lines += [f"{toml_quote(pattern)} = {toml_list(sorted(codes))}" for pattern, codes in sorted(ignores.items())]
    lines += _ruff_limit_tables({**policy.preset.ruff_limits, **{key: overrides[key] for key in _RUFF_LIMITS if key in overrides}})
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class GraphFacts:
    names: Sequence[str]
    arbor_index: Mapping[str, tuple[str | None, str | None]]
    scope_by_module: Mapping[str, str]
    packages: PackageBoundaries | None = None


def generate(
    policy: Policy, facts: GraphFacts, checkers: Sequence[TypeChecker], renderers: Sequence[ContractRenderer],
) -> dict[str, str]:
    """Every generated file, keyed by posix path relative to the policy root."""
    return _generate(policy, facts, checkers, renderers)


def generate_for_scan(
    policy: Policy, scan: ScanResult, checkers: Sequence[TypeChecker], renderers: Sequence[ContractRenderer],
    packages: PackageBoundaries | None = None,
) -> dict[str, str]:
    """``generate`` for a scanned repository; ``packages`` adds the package assignment to the contracts."""
    return _generate(policy, GraphFacts(sorted(scan.known), scan.arbor_index, scan.scope_by_module, packages), checkers, renderers)


def _generate(
    policy: Policy, facts: GraphFacts, checkers: Sequence[TypeChecker], renderers: Sequence[ContractRenderer],
) -> dict[str, str]:
    prefix = "" if policy.output_dir in ("", ".") else policy.output_dir.rstrip("/") + "/"
    paths = sorted(set(policy.sources))
    names, arbor_index, scope_by_module = facts.names, facts.arbor_index, facts.scope_by_module
    contracts = compile_contracts(policy, names, arbor_index, scope_by_module, facts.packages)
    files = {prefix + renderer.config_file: renderer.render_contracts(contracts) for renderer in renderers}
    files[prefix + "ruff.toml"] = ruff(policy, names, arbor_index, scope_by_module)
    for checker in checkers:
        files[prefix + checker.config_file] = checker.render_config(policy, paths)
    return files
