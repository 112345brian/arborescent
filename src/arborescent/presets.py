"""Presets: named bundles of how much Arborescent enforces. ``strict`` is the default and the reference; the others only
relax it. Pure data; ``Policy`` holds the chosen preset and the rules and generators ask it, so no check hard-codes
how strict it is. Also the vendored trait catalog: the names a ring's ``traits`` list may use."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Literal

from .effect_categories import (
    EFFECT_CATEGORIES,
    INJECTABLE_EFFECTS,
    MODULE_STATE_CATEGORIES,
)

__all__ = [
    "AT_IMPORT_TRAITS", "CATEGORY_TRAITS", "DEFAULT_FORBID_CATEGORIES", "DEFAULT_PRESET", "DEFAULT_TAINT_SEEDS", "EFFECT_CATEGORIES",
    "EFFECT_TRAITS", "INJECTABLE_EFFECTS", "MODULE_STATE_CATEGORIES", "PERMITS_EFFECTS", "PERMITS_OS_BRANCHING", "PRESETS",
    "REQUIRE_ABSTRACT", "REQUIRE_TYPED_PORTS", "TAINT_SEED_CATEGORIES", "TRAITS", "Preset", "Scope", "trait_of",
]

# The trait catalog (#222, #223). Vendored: a project cannot define or redefine a trait, so a name means the same everywhere.
# Spelled out one category at a time on purpose: a new effect category needs a name chosen here (tests/test_traits.py).
CATEGORY_TRAITS: Mapping[str, str] = {
    "clock": "no-clock",
    "env": "no-env",
    "identity": "no-identity",
    "randomness": "no-randomness",
    "sleep": "no-sleep",
    "threads": "no-threads",
    "process": "no-process",
    "database": "no-database",
    "network": "no-network",
    "filesystem": "no-filesystem",
    "dynamic-import": "no-dynamic-import",
    "patch": "no-patch",
    "console": "no-console",
    "global": "no-global",
    "state": "no-sync-primitives",  # the category covers `threading` primitives only, so the trait does not say "state"
    "monotonic": "no-monotonic",
    "host": "no-host",
    "fs-query": "no-fs-query",
    "registry": "no-registry",
    "cache": "no-cache",
}

PERMITS_EFFECTS = "permits-effects"  # the explicit "allow all": no call-time `no-*` trait may stand beside it
PERMITS_OS_BRANCHING = "permits-os-branching"
REQUIRE_ABSTRACT = "require-abstract"
REQUIRE_TYPED_PORTS = "require-typed-ports"

# Trait -> the categories it forbids. A ring that holds any of these replaces its positional effect default.
# `no-effects` is every category, so it grows when a release adds one.
EFFECT_TRAITS: Mapping[str, frozenset[str]] = {
    **{trait: frozenset({category}) for category, trait in CATEGORY_TRAITS.items()},
    "no-module-state": frozenset({"global", "registry", "cache"}),
    "no-effects": EFFECT_CATEGORIES,
    PERMITS_EFFECTS: frozenset[str](),
}

# Trait -> the categories it forbids while a module is imported (#230). These leave the positional effect default alone.
AT_IMPORT_TRAITS: Mapping[str, frozenset[str]] = {
    **{f"{trait}-at-import": frozenset({category}) for category, trait in CATEGORY_TRAITS.items()},
    "no-effects-at-import": EFFECT_CATEGORIES,
}

TRAITS: frozenset[str] = frozenset({*EFFECT_TRAITS, *AT_IMPORT_TRAITS, PERMITS_OS_BRANCHING, REQUIRE_ABSTRACT, REQUIRE_TYPED_PORTS})


def trait_of(category: str) -> str:
    """The single-category trait that forbids ``category``."""
    return CATEGORY_TRAITS[category]


# What a ring forbids when the policy does not say (the strict preset's middle rings, and the innermost ring): the categories that
# existed before 0.8.0. Opt in to the rest with their `no-*` traits.
DEFAULT_FORBID_CATEGORIES: frozenset[str] = EFFECT_CATEGORIES - {"state", "monotonic", "host", "fs-query"} - MODULE_STATE_CATEGORIES

# #98 (function-level taint): which effect categories count as an I/O primitive a function can be seeded with.
# ``patch`` (test-only) and ``global`` (module-state mutation, not I/O) are excluded.
TAINT_SEED_CATEGORIES: frozenset[str] = EFFECT_CATEGORIES - {"patch", "global", "state"} - MODULE_STATE_CATEGORIES
DEFAULT_TAINT_SEEDS: tuple[str, ...] = tuple(sorted(DEFAULT_FORBID_CATEGORIES - {"patch", "global"}))

# Which imports outside the project need an entry in the importing ring's ``external`` allow-list.
#   "all"          every one, stdlib included
#   "io"           third-party packages and the I/O stdlib modules (``IO_MODULES``); other stdlib is free
#   "third-party"  third-party packages only; the whole stdlib is free
Scope = Literal["all", "io", "third-party"]

# Stdlib modules that mean "I/O, process or OS". Under scope "all" and "io" a ring must list them to import them; the
# import-linter contracts ban them per ring because import-linter has deny-lists only. A policy may replace the set
# with ``io_modules = [...]``.
IO_MODULES: frozenset[str] = frozenset(
    {
        "os", "subprocess", "socket", "shutil", "tempfile", "urllib", "http", "sqlite3", "threading",
        "multiprocessing", "asyncio", "ctypes", "webbrowser", "signal", "platform", "getpass", "pathlib", "io",
    }
)  # fmt: skip


# Structural, type-level stdlib every ring may import under any scope (#128): they are how a pure data model is written
# at all and depend on nothing outside the language. ``collections`` covers ``collections.abc``.
STRUCTURAL_STDLIB: frozenset[str] = frozenset({"__future__", "typing", "dataclasses", "collections", "enum", "abc"})
@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    external_scope: Scope
    os_branching: bool  # True: only rings marked ``os_branching`` may read os.name / sys.platform
    middle_forbid: frozenset[str]
    git_hook: bool = False  # True: configuring or checking a repo also installs the pre-push hook that runs ``check``
    ruff_select: tuple[str, ...] = ()  # Ruff rules the generated ruff.toml selects on top of the shared set
    ruff_limits: Mapping[str, int] = field(default_factory=dict)  # function-size limits the generated ruff.toml sets, keyed like `[tool.arborescent.ruff]`

    @property
    def bans_io(self) -> bool:
        """Whether the generated import-linter contracts ban the I/O modules a ring does not list."""
        return self.external_scope != "third-party"

    def restricts(self, top: str, is_stdlib: bool, io_modules: Collection[str]) -> bool:
        """Whether importing external package ``top`` requires an allow-list entry."""
        if is_stdlib and top in STRUCTURAL_STDLIB:
            return False
        if self.external_scope == "all":
            return True
        if not is_stdlib:
            return True
        return self.external_scope == "io" and top in io_modules


# Small functions are the cheapest architecture there is: each limit is tighter than Ruff's default (complexity 10, branches 12,
# statements 50) except `max-args` and `max-returns`, which restate it so the rule is on and the number is visible in ruff.toml.
STRICT_RUFF_LIMITS: Mapping[str, int] = {"max-complexity": 8, "max-args": 5, "max-branches": 10, "max-returns": 6, "max-statements": 40}

PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            "strict", "every external import, stdlib included, must be allow-listed per ring; OS branching limited", "all", True, DEFAULT_FORBID_CATEGORIES,
            git_hook=True, ruff_select=("PLR0911",), ruff_limits=STRICT_RUFF_LIMITS,
        ),
        Preset("standard", "third-party and I/O stdlib must be allow-listed; other stdlib is free; OS branching limited", "io", True, frozenset({"env", "sleep", "threads", "process"})),
        Preset("relaxed", "only third-party packages are allow-listed; layering and classification still enforced", "third-party", False, frozenset()),
    )
}
DEFAULT_PRESET = "strict"
