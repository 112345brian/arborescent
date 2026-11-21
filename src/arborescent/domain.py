"""The Arborescent vocabulary: the policy, a violation, and what a tool run returned. Pure; no I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from . import policy_imports
from .architecture_policy import (
    DISABLED_TOOL_NAMES,
    ArchitecturePolicy,
    ArchitectureScope,
    validate_architecture,
)
from .coupling_policy import CouplingPolicy
from .effects_policy import EffectsPolicy
from .package_constraints import PackageConstraints
from .policy_types import (
    ALL,
    ConstructionSite,
    DocsPolicy,
    OwnedLiteral,
    PolicyError,
    ResultDiscard,
    RingRule,
    in_unit,
    normalized_source_path,
    path_under,
)
from .presets import DEFAULT_TAINT_SEEDS, EFFECT_CATEGORIES
from .purity_policy import PurityPolicy
from .stdlib import SUPPORTED_PYTHONS, stdlib_for

__all__ = [
    "ALL",
    "CONSTRAINT_RULES",
    "DISABLED_TOOL_NAMES",
    "EFFECT_CATEGORIES",
    "EFFECT_RULES",
    "INFORMATIONAL_RULES",
    "NOISE_DIRS",
    "TACH_EXCLUDED_FOLDERS",
    "Allowance",
    "ArchitectureContracts",
    "ArchitecturePolicy",
    "ArchitectureScope",
    "ChildArbor",
    "ConstructionSite",
    "Contract",
    "DocsPolicy",
    "ModuleLayer",
    "Mutation",
    "OwnedLiteral",
    "Policy",
    "PolicyError",
    "RatchetSpec",
    "ResultDiscard",
    "RingRule",
    "Rule",
    "ToolResult",
    "Violation",
    "dist_name",
    "in_unit",
    "path_under",
]

NOISE_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules", ".arborescent", ".mypy_cache", ".ruff_cache", ".tox"})
# Folders no walker (scanner, practices, selftest copy) looks into. Practices add build output on top of this.

TACH_EXCLUDED_FOLDERS = ("__pycache__", "tests", ".venv", "venv", "node_modules")
# The generated tach.toml `exclude` (engines.py); tach_visibility reads the same names, so the two never drift apart.

class Rule(StrEnum):
    """Every rule id, in one place. The values are stored in baseline files (``rule|subject|detail``): never rename one."""

    UNCLASSIFIED = "unclassified"
    FORBIDDEN_MODULE = "forbidden-module"
    NOISE_DIR_CODE = "noise-dir-code"
    VENDOR_FOLDER = "vendor-folder"
    WILDCARD_RING = "wildcard-ring"
    SUPPRESSION = "suppression"
    DISCARD = "discard"
    LAZY_IMPORT = "lazy-import"
    RESULT_DISCARD = "result-discard"
    EFFECT_CLOCK = "effect-clock"
    EFFECT_ENV = "effect-env"
    EFFECT_IDENTITY = "effect-identity"
    EFFECT_RANDOMNESS = "effect-randomness"
    EFFECT_SLEEP = "effect-sleep"
    EFFECT_THREADS = "effect-threads"
    EFFECT_PROCESS = "effect-process"
    DEV_TOOL_IMPORT = "dev-tool-import"
    DEV_TOOL_MISPLACED = "dev-tool-misplaced"
    DEV_TOOL_PRODUCT_IMPORT = "dev-tool-product-import"
    EFFECT_DATABASE = "effect-database"
    EFFECT_NETWORK = "effect-network"
    EFFECT_FILESYSTEM = "effect-filesystem"
    EFFECT_DYNAMIC_IMPORT = "effect-dynamic-import"
    EFFECT_PATCH = "effect-patch"
    EFFECT_CONSOLE = "effect-console"
    EFFECT_GLOBAL = "effect-global"
    EFFECT_STATE = "effect-state"
    EFFECT_MONOTONIC = "effect-monotonic"
    EFFECT_HOST = "effect-host"
    EFFECT_FS_QUERY = "effect-fs-query"
    EFFECT_UNSANCTIONED_IMPORT = "effect-unsanctioned-import"
    EFFECT_VIA_IMPORT = "effect-via-import"
    INTERNAL_IMPORT = "internal-import"
    FORWARDED_IMPORT = "forwarded-import"
    RESTRICTED_IMPORT = "restricted-import"
    LITERAL_OUTSIDE_OWNER = "literal-outside-owner"
    CONSTRUCTION_OUTSIDE_SITE = "construction-outside-site"
    MISSING_MODULE_DOCSTRING = "missing-module-docstring"
    DOCSTRING_RING_MISMATCH = "docstring-ring-mismatch"
    NON_ABSTRACT_PORT = "non-abstract-port"
    WEAKLY_TYPED_PORT = "weakly-typed-port"
    REEXPORT_RATIO = "reexport-ratio"
    MIXED_PURITY = "mixed-purity"
    MIXED_PURITY_EXCLUDED = "mixed-purity-excluded"
    UNTOKENIZABLE_SOURCE = "untokenizable-source"
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
    STALE_CONFIG_PATH = "stale-config-path"
    SOURCE_ROOT_DRIFT = "source-root-drift"
    TEST_REFERENCE = "test-reference"
    ADAPTER_EQUIVALENCE = "adapter-equivalence"
    ADAPTER_EQUIVALENCE_REPORT = "adapter-equivalence-report"
    INVALID_ARBOR_ANNOTATION = "invalid-arbor-annotation"
    ARBOR_RING_MISMATCH = "arbor-ring-mismatch"
    UNANNOTATED_MODULE = "unannotated-module"
    CLASSIFY_FACTS = "classify-facts"
    # package assignment (`package_catalog`): which package owns each module and asset
    PACKAGE_UNASSIGNED_MODULE = "package-unassigned-module"
    PACKAGE_MULTIPLY_ASSIGNED_MODULE = "package-multiply-assigned-module"
    PACKAGE_UNOWNED_ASSET = "package-unowned-asset"
    PACKAGE_MULTIPLY_ASSIGNED_ASSET = "package-multiply-assigned-asset"
    PACKAGE_SPANS_RINGS = "package-spans-rings"
    PACKAGE_UNDECLARED_DEPENDENCY = "package-undeclared-dependency"
    PACKAGE_DEPENDENCY_CYCLE = "package-dependency-cycle"
    PACKAGE_UNUSED_DEPENDENCY = "package-unused-dependency"
    PACKAGE_OWNS_EVERYTHING = "package-owns-everything"
    PACKAGE_NEVER_DEPENDED_ON = "package-never-depended-on"
    TACH_INVISIBLE_IMPORT = "tach-invisible-import"
    # annotation preservation (`package_parts`): a split part keeps the catalog's annotations and public contracts
    PACKAGE_SEMANTICS_DRIFT = "package-semantics-drift"
    PACKAGE_MEMBERSHIP_DRIFT = "package-membership-drift"
    PACKAGE_RING_DRIFT = "package-ring-drift"
    PACKAGE_PUBLIC_SYMBOL_DROPPED = "package-public-symbol-dropped"
    PACKAGE_PRIVATE_IMPORT = "package-private-import"
    PACKAGE_RELEASE_POLICY_MISSING = "package-release-policy-missing"
    PACKAGE_PYPROJECT_DRIFT = "package-pyproject-drift"
    # package constraints (`[package.constraints]`, #165): a promise about effects and imports, enforced on facts and on placement
    PACKAGE_CONSTRAINT_FACTS = "package-constraint-facts"
    PACKAGE_CONSTRAINT_PLACEMENT = "package-constraint-placement"


EFFECT_RULES: Mapping[str, Rule] = {
    "clock": Rule.EFFECT_CLOCK,
    "env": Rule.EFFECT_ENV,
    "identity": Rule.EFFECT_IDENTITY,
    "randomness": Rule.EFFECT_RANDOMNESS,
    "sleep": Rule.EFFECT_SLEEP,
    "threads": Rule.EFFECT_THREADS,
    "process": Rule.EFFECT_PROCESS,
    "database": Rule.EFFECT_DATABASE,
    "network": Rule.EFFECT_NETWORK,
    "filesystem": Rule.EFFECT_FILESYSTEM,
    "dynamic-import": Rule.EFFECT_DYNAMIC_IMPORT,
    "patch": Rule.EFFECT_PATCH,
    "console": Rule.EFFECT_CONSOLE,
    "global": Rule.EFFECT_GLOBAL,
    "state": Rule.EFFECT_STATE,
    "monotonic": Rule.EFFECT_MONOTONIC,
    "host": Rule.EFFECT_HOST,
    "fs-query": Rule.EFFECT_FS_QUERY,
}

# Not catalog gaps despite the prefix: a constraint is a promise, so `modularity = "report"` never demotes these.
CONSTRAINT_RULES: frozenset[Rule] = frozenset({Rule.PACKAGE_CONSTRAINT_FACTS, Rule.PACKAGE_CONSTRAINT_PLACEMENT})

# Findings that are reported but never fail a check and never enter the baseline: a package spanning rings is normal.
# A package's pyproject.toml is owner-edited, so a differing derived field is advice, not a failure (like generated-preset drift).
INFORMATIONAL_RULES: frozenset[Rule] = frozenset({
    Rule.PACKAGE_SPANS_RINGS, Rule.PACKAGE_PYPROJECT_DRIFT, Rule.PACKAGE_UNUSED_DEPENDENCY, Rule.PACKAGE_OWNS_EVERYTHING, Rule.PACKAGE_NEVER_DEPENDED_ON,
    Rule.TACH_INVISIBLE_IMPORT, Rule.CLASSIFY_FACTS, Rule.TEST_REFERENCE, Rule.MIXED_PURITY_EXCLUDED,
    Rule.ADAPTER_EQUIVALENCE_REPORT,
})


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
    target: str = field(default="", compare=False)  # the imported module, for import findings; not part of the key
    hint: str = field(default="", compare=False)  # extra advice appended when rendered (#133); not part of the key

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.subject}|{self.detail}"

    def render(self) -> str:
        where = f"{self.subject}:{self.line}" if self.line else self.subject
        hint = _RENDER_HINTS.get(self.rule, "")
        return f"{where}: [{self.rule}] {self.detail}{hint}{self.hint}"


_RENDER_HINTS = {
    Rule.LAZY_IMPORT: " (put it at module level, or record why it stays with `# deferred: <reason>` on or above the line)",
    Rule.VENDOR_FOLDER: (
        " (folder excluded from the scan; to govern it list it under `vendor_roots` or set `scan_vendors = true`,"
        " otherwise first-party imports of it are reported as external)"
    ),
}


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
class RatchetSpec:
    """A named ratchet (#147): a per-module counter that may only go down. ``measure`` says how it is counted."""

    name: str
    measure: str  # "rule" (findings of one rule id) | "regex" (matches in the source) | "ast_call" (calls of a dotted name) | "command"
    argument: tuple[str, ...]  # the rule id, the pattern or the dotted callee (one item); the command's argv
    modules: tuple[str, ...] = ()  # module globs (the `scope` key in `[ratchet.<name>]`); empty means every scanned module
    baseline: str = "arborescent-ratchets.json"


@dataclass(frozen=True)
class ChildArbor:
    """A nested package root whose own direct Arborescent policy owns its source."""

    path: str


@dataclass(frozen=True)
class Policy(ArchitecturePolicy):
    """The single source of truth that the three tool configs and Arborescent's own checks derive from.

    The inherited `ArchitecturePolicy` fields are the root's own rule data (an unscoped repository's only scope; empty
    rings under `scopes`, where each `ArchitectureScope` holds its own). The fields below are whole-run tool
    configuration, `config._SCOPED_ROOT_GLOBALS`: they exist once per invocation and are never per scope."""

    exclude: tuple[str, ...] = ()
    scan_vendors: bool = False
    scan_vendor_paths: tuple[str, ...] = ()  # vendor_roots: scanned even when scan_vendors is off
    child_policy_mode: str = "certify"  # "certify" keeps child-owned policy; "parent" overrides it
    dev_tools_forbid: tuple[str, ...] = ("process",)  # effect categories tooling may not use (raw subprocess by default)
    dev_tools_may_import: tuple[str, ...] = ()  # globs of product modules tooling may import; empty means any
    dev_tools: tuple[str, ...] = ()  # repository-relative folders or files that are developer tooling: no ring, may import product code, never imported by it
    python: str = "3.11"
    type_checkers: tuple[str, ...] = ("ty",)
    output_dir: str = "."
    baseline: str = "arborescent-baseline.json"
    ratchet_files: tuple[str, ...] = ()  # numeric ratchets checked by `check --growth-against`
    suggestion_threshold: int = 300
    ruff_overrides: Mapping[str, Any] = field(default_factory=lambda: {})
    customization_labels: tuple[str, ...] = ()
    practices: Mapping[str, Any] = field(default_factory=lambda: {})
    scopes: tuple[ArchitectureScope, ...] = ()
    children: tuple[ChildArbor, ...] = ()
    package_catalog: str | None = None  # opt-in: packages.toml path; every module and asset must belong to one package
    package_parts: tuple[str, ...] = ()  # opt-in: extracted part trees (inside the repository) checked against the catalog
    package_pyproject: bool = False  # opt-in, needs package_catalog: derive each package's pyproject.toml fields (#60)
    modularity: str = "ready"  # "ready": package gaps fail `check`; "report": they are notes (needs package_catalog). Independent of the lint preset
    mixed_purity: PurityPolicy = field(default_factory=PurityPolicy)  # #173: thresholds, effect kinds and the reasoned allowlist of `mixed-purity`
    coupling: CouplingPolicy = field(default_factory=CouplingPolicy)  # #148: entrypoints and hub threshold for `arborescent coupling`
    package_constraints: PackageConstraints = field(default_factory=PackageConstraints)  # #164: `[package.constraints]`, parsed only, not enforced
    ratchets: tuple[RatchetSpec, ...] = ()  # #147: `[ratchet.<name>]` counters compared with their baseline by `check`
    effects: EffectsPolicy = field(default_factory=EffectsPolicy)  # 0.8.0: sanctioned seam modules and the guard that goes with them
    taint_seeds: tuple[str, ...] = DEFAULT_TAINT_SEEDS  # #98: effect categories (plus "network") a function-level call can be seeded with

    def __post_init__(self) -> None:
        _validate_package_catalog(self.package_catalog)
        _validate_modularity(self.modularity, self.package_catalog)
        _validate_package_parts(self.package_parts, self.package_catalog)
        if self.package_pyproject and self.package_catalog is None:
            raise PolicyError("`package_pyproject` derives from the packages in `package_catalog`, which is not set")
        validate_architecture(self, has_scopes=bool(self.scopes))  # in place of `ArchitecturePolicy.__post_init__`
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

    # The ring/annotation/import/effect queries are inherited from `ArchitecturePolicy` (answering for the root's own
    # rule data); only the queries over whole-run configuration (the scope registry, the dev-tool rules) live here.

    def scope(self, name: str) -> ArchitectureScope:
        """Return a named scope or fail with a policy error."""
        for s in self.scopes:
            if s.name == name:
                return s
        raise PolicyError(f"unknown scope {name!r}")

    def scope_for_path(self, path: str) -> ArchitectureScope | None:
        """Find the scope whose source path owns a repository-relative file path."""
        normalized = normalized_source_path(path)
        for s in self.scopes:
            if any(_path_contains(source, normalized) for source in s.policy.sources):
                return s
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

    def scope_annotation_required_violation(
        self, scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str,
    ) -> str | None:
        """Validate `require_arbor_annotations` against only its owning scope's policy."""
        return self.scope(scope_name).policy.annotation_required_violation(arbor_index, module)

    def tool_may_import(self, target: str) -> bool:
        return policy_imports.tool_may_import(self.dev_tools_may_import, target)

    def is_dev_tool_path(self, rel: str) -> bool:
        """Whether a repository-relative path is, or lies below, a `dev_tools` entry."""
        return policy_imports.is_dev_tool_path(self.dev_tools, rel)

_NORMALIZED_SOURCES: dict[str, str] = {}


def _normalized_source(value: str) -> str:
    """Memoized for configured source roots only (a small fixed set); per-file paths must not enter this cache."""
    cached = _NORMALIZED_SOURCES.get(value)
    if cached is None:
        cached = _NORMALIZED_SOURCES[value] = normalized_source_path(value)
    return cached


def _path_contains(source: str, path: str) -> bool:
    source = _normalized_source(source)
    return source == "." or path == source or path.startswith(source + "/")


def _validate_package_catalog(value: str | None) -> None:
    if value is None:
        return
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise PolicyError(f"`package_catalog` must be a relative path inside the repository, got {value!r}")


MODULARITY_MODES: tuple[str, ...] = ("ready", "report")


def _validate_modularity(value: str, catalog: str | None) -> None:
    if value not in MODULARITY_MODES:
        raise PolicyError(f"`modularity` must be one of {list(MODULARITY_MODES)}, got {value!r}")
    if value == "report" and catalog is None:
        raise PolicyError("`modularity = \"report\"` needs `package_catalog`: there are no package gaps to report without one")


def _validate_package_parts(parts: tuple[str, ...], catalog: str | None) -> None:
    if not parts:
        return
    if catalog is None:
        raise PolicyError("`package_parts` needs `package_catalog`: the parts are checked against the catalog")
    for value in parts:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value or value in (".", "./"):
            raise PolicyError(f"`package_parts` must list relative paths inside the repository, got {value!r}")
    if len(set(parts)) != len(parts):
        raise PolicyError("`package_parts` must not list a path twice")


def _validate_suggestion_threshold(value: int) -> None:
    if value < 1:
        raise PolicyError("`suggestion_threshold` must be a positive number of logical code lines")


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
            normalized = normalized_source_path(source)
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


# (names that are not third-party-free for the module, names its ring lists); ``None`` for a module with no ring.
Allowance = tuple[frozenset[str], frozenset[str]]
Contract = tuple[str, list[str], list[str]]  # (name, source modules, forbidden modules)


@dataclass(frozen=True)
class ModuleLayer:
    """One module in the neutral contracts: the layer it sits in and, when the boundaries are listed per module, who it may import."""

    path: str
    layer: str
    may_import: tuple[str, ...] | None = None  # ``None``: the layer order alone decides


@dataclass(frozen=True)
class ArchitectureContracts:
    """What the policy requires of the import graph, in no tool's format. The engine adapters render it (import-linter,
    tach) or are handed it (any graph analyzer the user supplies); the core never writes a tool's configuration itself.

    Paths are relative to the repository root; the adapter that writes a file relative to ``config_dir`` does the offsetting.
    """

    root_packages: tuple[str, ...]
    forbidden: tuple[Contract, ...]  # (name, importers, modules they may not import), in the order they are reported
    layers: tuple[str, ...]  # outermost first
    modules: tuple[ModuleLayer, ...]
    explicit_boundaries: bool  # ``modules`` carry ``may_import``: same-layer peers and package boundaries are listed, not implied
    source_roots: tuple[str, ...]
    config_dir: str  # where the generated files go, relative to the repository root ("" = the root)
    distributions: tuple[tuple[str, str], ...]  # (module, distribution) where the import name differs from the package name
    external_exclude: tuple[str, ...]  # third-party imports no tool should demand a declaration for


@dataclass(frozen=True)
class Mutation:
    """A selftest mutation: text appended to one file (or a new file) that must make one rule newly fire."""

    rule: Rule  # the rule id that must newly fire
    label: str
    file: str  # posix path relative to the root
    append: str  # text appended to the file ("" for an added file: see ``new_file``)
    new_file: bool = False
    content: str = "X = 1\n"  # what an added file holds
