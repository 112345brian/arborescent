"""The ArchLint vocabulary: the policy, a violation, and what a tool run returned. Pure; no I/O."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .presets import DEFAULT_PRESET, IO_MODULES, PRESETS, Preset
from .stdlib import SUPPORTED_PYTHONS, stdlib_for

__all__ = [
    "ALL",
    "NOISE_DIRS",
    "Policy",
    "PolicyError",
    "RingRule",
    "Rule",
    "ToolResult",
    "Violation",
    "dist_name",
]

NOISE_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules", ".archlint", ".mypy_cache", ".ruff_cache", ".tox"})
# Folders no walker (scanner, practices, selftest copy) looks into. Practices add build output on top of this.

ALL = "*"  # in `may_import` or `external`: no restriction


class Rule(StrEnum):
    """Every rule id, in one place. The values are stored in baseline files (``rule|subject|detail``): never rename one."""

    UNCLASSIFIED = "unclassified"
    INTERNAL_IMPORT = "internal-import"
    EXTERNAL_IMPORT = "external-import"
    OS_BRANCHING = "os-branching"
    BAD_IMPORT = "bad-import"
    UNDECLARED_ENTRY_DEPENDENCY = "undeclared-entry-dependency"
    # repository practices (`[tool.archlint.practices]` keys use the same ids)
    GITIGNORE_SHADOW = "gitignore-shadow"
    SIBLING_VERSIONS = "sibling-versions"
    SECRETS_GUARD = "secrets-guard"
    DOCS_SYNC = "docs-sync"
    CRLF = "crlf"
    UNDECLARED_IMPORTS = "undeclared-imports"


class PolicyError(ValueError):
    """The policy is malformed or contradicts itself (reported, never silently repaired)."""


def dist_name(name: str) -> str:
    """PEP 503 normalised distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Violation:
    """One broken rule. ``key`` is stable across line moves so a baseline can hold it."""

    rule: str
    subject: str
    detail: str
    line: int = 0

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.subject}|{self.detail}"

    def render(self) -> str:
        where = f"{self.subject}:{self.line}" if self.line else self.subject
        return f"{where}: [{self.rule}] {self.detail}"


@dataclass(frozen=True)
class ToolResult:
    """What an external engine did. ``returncode`` is None when it could not be started."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0

    @property
    def started(self) -> bool:
        return self.returncode is not None


@dataclass(frozen=True)
class RingRule:
    name: str
    may_import: frozenset[str]  # internal rings (its own is implicit); ALL = every inner ring
    external: frozenset[str]  # top-level third-party/stdlib names; ALL = unrestricted
    os_branching: bool = False


@dataclass(frozen=True)
class Policy:
    """The single source of truth that the three tool configs and ArchLint's own checks derive from."""

    rings: tuple[RingRule, ...]  # innermost first
    classify: tuple[tuple[str, str], ...]  # (glob on dotted module name, ring); first match wins
    sources: tuple[str, ...] = (".",)
    source_roots: tuple[str, ...] = (".",)
    exclude: tuple[str, ...] = ()
    entry_scripts: tuple[str, ...] = ()  # dotted module names that are PEP 723 scripts
    dependencies: Mapping[str, str] = field(default_factory=lambda: {})  # import name -> distribution
    forbid_everywhere: tuple[str, ...] = ()
    python: str = "3.11"
    type_checkers: tuple[str, ...] = ("pyright",)
    output_dir: str = "."
    baseline: str = "archlint-baseline.json"
    practices: Mapping[str, Any] = field(default_factory=lambda: {})
    preset: Preset = PRESETS[DEFAULT_PRESET]
    io_modules: frozenset[str] = IO_MODULES  # stdlib modules that mean "I/O, process or OS"

    def __post_init__(self) -> None:
        names = [r.name for r in self.rings]
        if not names or len(set(names)) != len(names):
            raise PolicyError("`rings` must list distinct ring names, innermost first")
        for i, rule in enumerate(self.rings):
            bad = rule.may_import - {ALL, *names[:i]}
            if bad:
                raise PolicyError(
                    f"ring {rule.name!r} may only import inner rings {names[:i]}, not {sorted(bad)} (onion rule)"
                )
        for pattern, ring in self.classify:
            if ring not in names:
                raise PolicyError(f"classify {pattern!r} names unknown ring {ring!r}")
        if self.python not in SUPPORTED_PYTHONS:
            raise PolicyError(f"`python` {self.python!r} is not supported; choose one of {list(SUPPORTED_PYTHONS)}")

    @property
    def stdlib(self) -> frozenset[str]:
        """Top-level stdlib module names of the policy's target Python, not of the interpreter running archlint."""
        return stdlib_for(self.python)

    def ring(self, name: str) -> RingRule:
        for r in self.rings:
            if r.name == name:
                return r
        raise PolicyError(f"unknown ring {name!r}")

    def ring_of(self, module: str) -> str | None:
        for pattern, ring in self.classify:
            if fnmatch.fnmatchcase(module, pattern):
                return ring
        return None

    def allowed_rings(self, ring: str) -> frozenset[str]:
        """The rings a module of ``ring`` may import: itself plus its declared inner rings."""
        rule = self.ring(ring)
        names = [r.name for r in self.rings]
        inner = names[: names.index(ring)]
        return frozenset({ring, *(inner if ALL in rule.may_import else rule.may_import & set(inner))})

    def restricts_external(self, top: str, is_stdlib: bool) -> bool:
        """Whether importing external package ``top`` needs an entry in the importing ring's allow-list."""
        return self.preset.restricts(top, is_stdlib, self.io_modules)

    def expressible_by_layers(self) -> bool:
        """True when every ring may import ALL inner rings, so tach's layers alone state the policy."""
        return all(self.allowed_rings(r.name) == frozenset(x.name for x in self.rings[: i + 1]) for i, r in enumerate(self.rings))
