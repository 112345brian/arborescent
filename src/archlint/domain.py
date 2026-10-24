"""The ArchLint vocabulary: the policy, a violation, and what a tool run returned. Pure; no I/O."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ALL",
    "Policy",
    "PolicyError",
    "RingRule",
    "ToolResult",
    "Violation",
    "dist_name",
]

ALL = "*"  # in `may_import` or `external`: no restriction


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
    dependencies: Mapping[str, str] = field(default_factory=dict)  # import name -> distribution
    forbid_everywhere: tuple[str, ...] = ()
    python: str = "3.11"
    type_checkers: tuple[str, ...] = ("pyright",)
    output_dir: str = "."
    baseline: str = "archlint-baseline.json"
    practices: Mapping[str, Any] = field(default_factory=dict)

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

    def expressible_by_layers(self) -> bool:
        """True when every ring may import ALL inner rings, so tach's layers alone state the policy."""
        return all(self.allowed_rings(r.name) == frozenset(x.name for x in self.rings[: i + 1]) for i, r in enumerate(self.rings))

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> Policy:
        groups: Mapping[str, Sequence[str]] = raw.get("groups", {})

        def expand(items: Sequence[str]) -> frozenset[str]:
            out: set[str] = set()
            for item in items:
                if item.startswith("@"):
                    if item[1:] not in groups:
                        raise PolicyError(f"unknown group {item!r}")
                    out.update(groups[item[1:]])
                else:
                    out.add(item)
            return frozenset(out)

        names = list(raw.get("rings", []))
        if not names or len(set(names)) != len(names):
            raise PolicyError("`rings` must list distinct ring names, innermost first")
        ring_tables: Mapping[str, Mapping[str, Any]] = raw.get("ring", {})
        for unknown in set(ring_tables) - set(names):
            raise PolicyError(f"[ring.{unknown}] is not in `rings`")
        rings: list[RingRule] = []
        for i, name in enumerate(names):
            table = ring_tables.get(name, {})
            may = expand(table.get("may_import", []))
            bad = may - {ALL, *names[:i]}
            if bad:
                raise PolicyError(
                    f"ring {name!r} may only import inner rings {names[:i]}, not {sorted(bad)} (onion rule)"
                )
            rings.append(RingRule(name, may, expand(table.get("external", [])), bool(table.get("os_branching", False))))
        classify = tuple((str(k), str(v)) for k, v in raw.get("classify", {}).items())
        for pattern, ring in classify:
            if ring not in names:
                raise PolicyError(f"classify {pattern!r} names unknown ring {ring!r}")
        checkers = tuple(raw.get("type_checkers", ["pyright"]))
        return Policy(
            rings=tuple(rings),
            classify=classify,
            sources=tuple(raw.get("sources", ["."])),
            source_roots=tuple(raw.get("source_roots", ["."])),
            exclude=tuple(raw.get("exclude", [])),
            entry_scripts=tuple(raw.get("entry_scripts", [])),
            dependencies=dict(raw.get("dependencies", {})),
            forbid_everywhere=tuple(raw.get("forbid_everywhere", [])),
            python=str(raw.get("python", "3.11")),
            type_checkers=checkers,
            output_dir=str(raw.get("output_dir", ".")),
            baseline=str(raw.get("baseline", "archlint-baseline.json")),
            practices=dict(raw.get("practices", {})),
        )
