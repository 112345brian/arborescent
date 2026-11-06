"""Presets: named bundles of how much Arborescent enforces. ``strict`` is the default and the reference; the others only
relax it. Pure data; ``Policy`` holds the chosen preset and the rules and generators ask it, so no check hard-codes
how strict it is."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["DEFAULT_PRESET", "EFFECT_CATEGORIES", "PRESETS", "Preset", "Scope"]

EFFECT_CATEGORIES: frozenset[str] = frozenset(
    {
        "clock", "env", "identity", "randomness", "sleep", "threads", "process", "database", "filesystem", "dynamic-import", "patch",
        "console", "global",
    }
)

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
            "strict", "every external import, stdlib included, must be allow-listed per ring; OS branching limited", "all", True, EFFECT_CATEGORIES,
            git_hook=True, ruff_select=("PLR0911",), ruff_limits=STRICT_RUFF_LIMITS,
        ),
        Preset("standard", "third-party and I/O stdlib must be allow-listed; other stdlib is free; OS branching limited", "io", True, frozenset({"env", "sleep", "threads", "process"})),
        Preset("relaxed", "only third-party packages are allow-listed; layering and classification still enforced", "third-party", False, frozenset()),
    )
}
DEFAULT_PRESET = "strict"
