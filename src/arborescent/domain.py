"""The Arborescent vocabulary: the policy, a violation, and what a tool run returned. Pure; no I/O."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .presets import DEFAULT_PRESET, EFFECT_CATEGORIES, IO_MODULES, PRESETS, Preset
from .stdlib import SUPPORTED_PYTHONS, stdlib_for

__all__ = [
    "ALL",
    "EFFECT_RULES",
    "NOISE_DIRS",
    "ArchitectureScope",
    "ChildArbor",
    "Policy",
    "PolicyError",
    "RingRule",
    "Rule",
    "ToolResult",
    "Violation",
    "dist_name",
]

NOISE_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules", ".arborescent", ".mypy_cache", ".ruff_cache", ".tox"})
DISABLED_TOOL_NAMES = frozenset({"import-linter", "tach", "ruff", "pyright", "basedpyright", "ty", "pyrefly", "arbor-annotations"})
# Folders no walker (scanner, practices, selftest copy) looks into. Practices add build output on top of this.

ALL = "*"  # in `may_import` or `external`: no restriction


class Rule(StrEnum):
    """Every rule id, in one place. The values are stored in baseline files (``rule|subject|detail``): never rename one."""

    UNCLASSIFIED = "unclassified"
    NOISE_DIR_CODE = "noise-dir-code"
    VENDOR_FOLDER = "vendor-folder"
    WILDCARD_RING = "wildcard-ring"
    SUPPRESSION = "suppression"
    DISCARD = "discard"
    EFFECT_CLOCK = "effect-clock"
    EFFECT_ENV = "effect-env"
    EFFECT_IDENTITY = "effect-identity"
    EFFECT_RANDOMNESS = "effect-randomness"
    EFFECT_SLEEP = "effect-sleep"
    EFFECT_THREADS = "effect-threads"
    EFFECT_PROCESS = "effect-process"
    EFFECT_DYNAMIC_IMPORT = "effect-dynamic-import"
    EFFECT_PATCH = "effect-patch"
    INTERNAL_IMPORT = "internal-import"
    SCOPE_IMPORT = "scope-import"
    SCOPE_EFFECT_LEAK = "scope-effect-leak"
    CIRCULAR_IMPORT = "circular-import"
    EXTERNAL_IMPORT = "external-import"
    OS_BRANCHING = "os-branching"
    BAD_IMPORT = "bad-import"
    UNDECLARED_ENTRY_DEPENDENCY = "undeclared-entry-dependency"
    # repository practices (`[tool.arborescent.practices]` keys use the same ids)
    GITIGNORE_SHADOW = "gitignore-shadow"
    SIBLING_VERSIONS = "sibling-versions"
    SECRETS_GUARD = "secrets-guard"
    DOCS_SYNC = "docs-sync"
    CRLF = "crlf"
    UNDECLARED_IMPORTS = "undeclared-imports"
    INVALID_ARBOR_ANNOTATION = "invalid-arbor-annotation"
    ARBOR_RING_MISMATCH = "arbor-ring-mismatch"


EFFECT_RULES: Mapping[str, Rule] = {
    "clock": Rule.EFFECT_CLOCK,
    "env": Rule.EFFECT_ENV,
    "identity": Rule.EFFECT_IDENTITY,
    "randomness": Rule.EFFECT_RANDOMNESS,
    "sleep": Rule.EFFECT_SLEEP,
    "threads": Rule.EFFECT_THREADS,
    "process": Rule.EFFECT_PROCESS,
    "dynamic-import": Rule.EFFECT_DYNAMIC_IMPORT,
    "patch": Rule.EFFECT_PATCH,
}


class PolicyError(ValueError):
    """The policy is malformed or contradicts itself (reported, never silently repaired)."""


def _validate_disabled_tools(tools: tuple[str, ...]) -> None:
    unknown = set(tools) - DISABLED_TOOL_NAMES
    if unknown:
        raise PolicyError(f"`disabled_tools` contains unknown tool names: {sorted(unknown)}")
    if len(set(tools)) != len(tools):
        raise PolicyError("`disabled_tools` must not contain duplicate names")


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
    forbid: frozenset[str] | None = None  # None means the positional preset default; [] explicitly allows all effects


@dataclass(frozen=True)
class ArchitectureScope:
    """One independently governed source tree and its import boundary."""

    name: str
    policy: Policy
    public: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildArbor:
    """A nested package root whose own direct Arborescent policy owns its source."""

    path: str


@dataclass(frozen=True)
class Policy:
    """The single source of truth that the three tool configs and Arborescent's own checks derive from."""

    rings: tuple[RingRule, ...]  # innermost first
    classify: tuple[tuple[str, str], ...]  # (glob on dotted module name, ring); first match wins
    sources: tuple[str, ...] = (".",)
    source_roots: tuple[str, ...] = (".",)
    exclude: tuple[str, ...] = ()
    scan_vendors: bool = False
    child_policy_mode: str = "certify"  # "certify" keeps child-owned policy; "parent" overrides it
    module_forbid_imports: tuple[tuple[str, tuple[str, ...]], ...] = ()
    entry_scripts: tuple[str, ...] = ()  # dotted module names that are PEP 723 scripts
    dependencies: Mapping[str, str] = field(default_factory=lambda: {})  # import name -> distribution
    forbid_everywhere: tuple[str, ...] = ()
    optional_imports: tuple[str, ...] = ()  # import names of optional extras; written to tach `[external] exclude`
    forbid_circular_imports: bool = True
    python: str = "3.11"
    type_checkers: tuple[str, ...] = ("ty",)
    output_dir: str = "."
    baseline: str = "arborescent-baseline.json"
    suggestion_threshold: int = 300
    ruff_overrides: Mapping[str, Any] = field(default_factory=lambda: {})
    customization_labels: tuple[str, ...] = ()
    practices: Mapping[str, Any] = field(default_factory=lambda: {})
    preset: Preset = PRESETS[DEFAULT_PRESET]
    io_modules: frozenset[str] = IO_MODULES  # stdlib modules that mean "I/O, process or OS"
    disabled_tools: tuple[str, ...] = ()
    scopes: tuple[ArchitectureScope, ...] = ()
    children: tuple[ChildArbor, ...] = ()

    def __post_init__(self) -> None:
        _validate_rings(self.rings, self.classify, bool(self.scopes))
        for pattern, targets in self.module_forbid_imports:
            if not pattern or not targets:
                raise PolicyError("`module_forbid_imports` needs non-empty module patterns and target lists")
        _validate_disabled_tools(self.disabled_tools)
        _validate_child_arbors(self.children)
        if self.child_policy_mode not in {"certify", "parent"}:
            raise PolicyError("`child_policy_mode` must be 'certify' or 'parent'")
        if self.python not in SUPPORTED_PYTHONS:
            raise PolicyError(f"`python` {self.python!r} is not supported; choose one of {list(SUPPORTED_PYTHONS)}")
        _validate_suggestion_threshold(self.suggestion_threshold)
        if self.scopes:
            _validate_scopes(self.scopes)

    @property
    def stdlib(self) -> frozenset[str]:
        """Top-level stdlib module names of the policy's target Python, not of the interpreter running arborescent."""
        return stdlib_for(self.python)

    def ring(self, name: str) -> RingRule:
        for r in self.rings:
            if r.name == name:
                return r
        raise PolicyError(f"unknown ring {name!r}")

    def scope(self, name: str) -> ArchitectureScope:
        """Return a named scope or fail with a policy error."""
        for scope in self.scopes:
            if scope.name == name:
                return scope
        raise PolicyError(f"unknown scope {name!r}")

    def scope_for_path(self, path: str) -> ArchitectureScope | None:
        """Find the scope whose source path owns a repository-relative file path."""
        normalized = _normalized_source_path(path)
        for scope in self.scopes:
            if any(_path_contains(source, normalized) for source in scope.policy.sources):
                return scope
        return None

    def resolve_scope_ring(
        self, scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str,
    ) -> str | None:
        """Resolve annotations against only the named scope's ring set."""
        return self.scope(scope_name).policy.resolve_ring(arbor_index, module)

    def scope_annotation_violation(
        self, scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str,
    ) -> str | None:
        """Validate a module annotation against only its owning scope's rings."""
        return self.scope(scope_name).policy.annotation_violation(arbor_index, module)

    def ring_of(self, module: str) -> str | None:
        for pattern, ring in self.classify:
            if fnmatch.fnmatchcase(module, pattern):
                return ring
        return None

    def resolve_ring(self, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
        """Resolve a module's annotated ring, falling back to ``classify`` when unavailable or invalid."""
        if "arbor-annotations" not in self.disabled_tools:
            annotation = arbor_index.get(name)
            if annotation is not None:
                ring, violation = annotation
                if violation is None and ring is not None and any(rule.name == ring for rule in self.rings):
                    return ring
        return self.ring_of(name)

    def annotation_violation(self, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
        """Return why a present annotation is invalid, independently of ring fallback."""
        if "arbor-annotations" in self.disabled_tools:
            return None
        annotation = arbor_index.get(name)
        if annotation is None:
            return None
        ring, violation = annotation
        if violation is not None:
            return violation
        if ring is not None and not any(rule.name == ring for rule in self.rings):
            return f"__arbor__ names unknown ring {ring!r}"
        return None

    def annotation_ring_mismatch(self, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
        """Return why a valid annotation's ring disagrees with what ``classify`` would assign, if it does."""
        if "arbor-annotations" in self.disabled_tools or self.annotation_violation(arbor_index, name) is not None:
            return None
        annotation = arbor_index.get(name)
        if annotation is None:
            return None
        ring, _ = annotation
        if ring is None:
            return None
        classify_ring = self.ring_of(name)
        if classify_ring is not None and classify_ring != ring:
            return f"__arbor__ names ring {ring!r}, but `classify` would assign {classify_ring!r}"
        return None

    def forbidden_imports(self, module: str) -> frozenset[str]:
        """Module-specific internal import restrictions, additive across matching globs."""
        return frozenset(target for pattern, targets in self.module_forbid_imports if fnmatch.fnmatchcase(module, pattern) for target in targets)

    def forbids_module_import(self, module: str, target: str) -> str | None:
        """Return the configured target restriction that matches an imported module, if any."""
        return next((banned for banned in sorted(self.forbidden_imports(module)) if target == banned or target.startswith(banned + ".") or fnmatch.fnmatchcase(target, banned)), None)

    def allowed_rings(self, ring: str) -> frozenset[str]:
        """The rings a module of ``ring`` may import: itself plus its declared inner rings."""
        rule = self.ring(ring)
        names = [r.name for r in self.rings]
        inner = names[: names.index(ring)]
        return frozenset({ring, *(inner if ALL in rule.may_import else rule.may_import & set(inner))})

    def forbidden_effects(self, ring: str) -> frozenset[str]:
        """Resolve a ring's effect policy; the outermost ring wins for a one-ring policy."""
        rule = self.ring(ring)
        position = next(i for i, candidate in enumerate(self.rings) if candidate.name == ring)
        forbidden: frozenset[str]
        if rule.forbid is not None:
            forbidden = rule.forbid
        elif position == len(self.rings) - 1:
            forbidden = frozenset[str]()
        elif position == 0:
            forbidden = EFFECT_CATEGORIES
        else:
            forbidden = self.preset.middle_forbid
        if forbidden and "dynamic-import" not in forbidden:
            forbidden = forbidden | frozenset[str]({"dynamic-import"})
        return forbidden

    def restricts_external(self, top: str, is_stdlib: bool) -> bool:
        """Whether importing external package ``top`` needs an entry in the importing ring's allow-list."""
        return self.preset.restricts(top, is_stdlib, self.io_modules)

    def expressible_by_layers(self) -> bool:
        """True when every ring may import ALL inner rings, so tach's layers alone state the policy."""
        return all(self.allowed_rings(r.name) == frozenset(x.name for x in self.rings[: i + 1]) for i, r in enumerate(self.rings))


def _validate_suggestion_threshold(value: int) -> None:
    if value < 1:
        raise PolicyError("`suggestion_threshold` must be a positive number of logical code lines")


def _validate_rings(rings: tuple[RingRule, ...], classify: tuple[tuple[str, str], ...], has_scopes: bool) -> None:
    names = [rule.name for rule in rings]
    if (not names and not has_scopes) or len(set(names)) != len(names):
        raise PolicyError("`rings` must list distinct ring names, innermost first")
    for index, rule in enumerate(rings):
        invalid = rule.may_import - {ALL, *names[:index]}
        if invalid:
            raise PolicyError(
                f"ring {rule.name!r} may only import inner rings {names[:index]}, not {sorted(invalid)} (onion rule)"
            )
        if rule.forbid is not None and (unknown := rule.forbid - EFFECT_CATEGORIES):
            raise PolicyError(f"ring {rule.name!r} forbids unknown effect categories {sorted(unknown)}")
    for pattern, ring in classify:
        if ring not in names:
            raise PolicyError(f"classify {pattern!r} names unknown ring {ring!r}")


def _normalized_source_path(value: str) -> str:
    path = PurePosixPath(value)
    return path.as_posix().removeprefix("./").rstrip("/") or "."


def _path_contains(source: str, path: str) -> bool:
    source = _normalized_source_path(source)
    return source == "." or path == source or path.startswith(source + "/")


def _validate_scopes(scopes: tuple[ArchitectureScope, ...]) -> None:
    names = [scope.name for scope in scopes]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise PolicyError("scope names must be non-empty and unique")
    _validate_scope_dependencies(scopes, set(names))
    _validate_scope_source_paths(scopes)
    _validate_scope_acyclic(scopes, names)


def _validate_child_arbors(children: tuple[ChildArbor, ...]) -> None:
    paths = [child.path for child in children]
    for path in paths:
        parsed = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        if (
            not path
            or parsed.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or path in {".", ".."}
            or ".." in parsed.parts
            or "\\" in path
            or parsed.as_posix() != path
        ):
            raise PolicyError(f"child arbor path {path!r} must be a normalized relative path beneath the parent")
    if len(set(paths)) != len(paths):
        raise PolicyError("child arbor paths must be unique")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if _path_contains(path, other) or _path_contains(other, path):
                raise PolicyError(f"child arbor paths may not overlap: {path!r} and {other!r}")


def _validate_scope_dependencies(scopes: tuple[ArchitectureScope, ...], known: set[str]) -> None:
    for scope in scopes:
        if len(scope.depends_on) != len(set(scope.depends_on)):
            raise PolicyError(f"scope {scope.name!r} has duplicate dependencies")
        for dependency in scope.depends_on:
            if dependency not in known:
                raise PolicyError(f"scope {scope.name!r} depends on unknown scope {dependency!r}")
            if dependency == scope.name:
                raise PolicyError(f"scope {scope.name!r} cannot depend on itself")


def _validate_scope_source_paths(scopes: tuple[ArchitectureScope, ...]) -> None:
    owned: list[tuple[str, str]] = []
    for scope in scopes:
        if not scope.policy.sources or not scope.policy.source_roots:
            raise PolicyError(f"scope {scope.name!r} requires non-empty `sources` and `source_roots`")
        for source in scope.policy.sources:
            if any(char in source for char in "*?["):
                raise PolicyError(f"scope source paths must be literal paths, got {source!r}")
            normalized = _normalized_source_path(source)
            for other_name, other_path in owned:
                if _path_contains(normalized, other_path) or _path_contains(other_path, normalized):
                    raise PolicyError(
                        f"scope source paths overlap: {other_name!r} owns {other_path!r} and "
                        f"{scope.name!r} owns {normalized!r}"
                    )
            owned.append((scope.name, normalized))


def _validate_scope_acyclic(scopes: tuple[ArchitectureScope, ...], names: list[str]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()
    by_name = {scope.name: scope for scope in scopes}

    def visit(name: str) -> None:
        if name in visiting:
            raise PolicyError(f"scope dependency cycle includes {name!r}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in by_name[name].depends_on:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)
