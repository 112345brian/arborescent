"""The ``[mixed_purity]`` policy table (#173): the thresholds, the effect kinds that count, and the reasoned allowlist."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MIN_PURE_UNITS = 2
DEFAULT_MIN_BODY_LINES = 4
# Effect categories that make a function effectful. ``os-branching`` is a portability fact and ``patch`` is test-only.
EXISTING_KINDS = (
    "clock", "env", "identity", "randomness", "sleep", "threads", "process", "database", "network", "filesystem", "dynamic-import", "console", "global",
)
NEW_KINDS = ("logging", "memo", "state")  # what #173 adds; they never reach the ``effect-*`` rules
# Detected since 0.8.0 and counted by ``mixed-purity`` only when ``kinds`` names them: the default set is unchanged.
OPTIONAL_KINDS = ("monotonic", "host", "fs-query")
AMBIENT = "ambient"  # a call matching a configured pattern (``ambient``); on only when patterns are configured
KNOWN_KINDS = frozenset((*EXISTING_KINDS, *NEW_KINDS, *OPTIONAL_KINDS, AMBIENT))
DEFAULT_KINDS = (*EXISTING_KINDS, *NEW_KINDS)


@dataclass(frozen=True)
class PurityAllow:
    """A module the rule leaves alone, and why. ``module`` is a dotted name, a submodule prefix or a glob."""

    module: str
    reason: str


@dataclass(frozen=True)
class PurityPolicy:
    """A module is mixed when it has at least ``min_pure_units`` substantial pure top-level functions or classes (a body of
    at least ``min_body_lines`` lines) and at least one effectful unit or import-time effect of a kind in ``kinds``."""

    enabled: bool = True
    report_excluded: bool = True  # parse files under `exclude` and report the mixed ones as notes; off skips that parse
    min_pure_units: int = DEFAULT_MIN_PURE_UNITS
    min_body_lines: int = DEFAULT_MIN_BODY_LINES
    kinds: tuple[str, ...] = DEFAULT_KINDS
    ambient: tuple[str, ...] = ()  # globs on the dotted name of an imported callable, e.g. ``myapp.state.state_root``
    allow: tuple[PurityAllow, ...] = ()
    exclude_rings: tuple[str, ...] = ()  # rings whose modules the rule skips: a ring that is meant to be effectful has nothing to split
    rings: tuple[str, ...] = ()  # when set, the only rings the rule looks at (#195)
    skip_unrestricted_rings: bool = False  # skip every ring whose effective `forbid` is empty: effects are allowed there (#195)
    weak_kinds: tuple[str, ...] = ()  # kinds that alone do not make a module worth splitting: such a module is a `mixed-purity-weak` note (#195)
