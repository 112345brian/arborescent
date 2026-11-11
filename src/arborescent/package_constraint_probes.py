"""Selftest probes for ``[package.constraints]`` (#165): a module that breaks the promise, and a module placed where the promise is not safe.

Both are new files beside an existing module, so neither can collide with a finding already recorded. ``package-constraint-facts`` must
newly fire for a module that uses a forbidden effect; ``package-constraint-placement`` for a module annotated into a ring that permits one.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import Mutation, Policy, Rule
from .package_constraint_rules import forbidden_by

__all__ = ["constraint_mutations"]

# One statement per effect category the probe can spell with a plain import (or none); the first forbidden category with a spelling is used.
_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("clock", "import time\n\n_ARBORESCENT_PROBE = time.time()\n"),
    ("randomness", "import random\n\n_ARBORESCENT_PROBE = random.random()\n"),
    ("sleep", "import time\n\ntime.sleep(0)\n"),
    ("env", "import os\n\n_ARBORESCENT_PROBE = os.getenv('ARBORESCENT_PROBE')\n"),
    ("identity", "import uuid\n\n_ARBORESCENT_PROBE = uuid.uuid4()\n"),
    ("process", "import subprocess\n\nsubprocess.run(['arborescent-probe'], check=False)\n"),
    ("console", "print('arborescent probe')\n"),
)
_FILE = "arborescent_probe_constraint.py"


def constraint_mutations(
    policy: Policy, paths: Mapping[str, str], scopes: Collection[tuple[Policy, frozenset[str]]],
    arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons one could not be built). ``scopes`` is each ladder with the modules it holds (the policy itself when unscoped)."""
    constraints = policy.package_constraints
    forbidden = forbidden_by(constraints)
    if not forbidden:
        return [], []
    out: list[Mutation] = []
    skipped: list[str] = []
    spelling = next((text for category, text in _SPELLINGS if category in forbidden), None)
    hosts = [(local, _host(local, known, paths, arbor_index)) for local, known in scopes]
    placed = [(local, host) for local, host in hosts if host is not None]
    if not placed:
        return [], ["package-constraint-facts: no classified module to place a probe beside"]
    if spelling is None:
        skipped.append(f"package-constraint-facts: no probe spells {sorted(forbidden)}")
    else:
        out.append(Mutation(Rule.PACKAGE_CONSTRAINT_FACTS, "a new module that uses a forbidden effect", _beside(paths[placed[0][1]]), "", new_file=True, content=spelling))
    for local, host in placed:
        ring = next((rule.name for rule in local.rings if forbidden - local.forbidden_effects(rule.name)), None)
        if ring is None:
            skipped.append(f"package-constraint-placement: every ring of {paths[host]} already forbids what the package forbids")
            continue
        out.append(Mutation(
            Rule.PACKAGE_CONSTRAINT_PLACEMENT, f"a new module annotated into the {ring} ring, which permits a forbidden effect",
            _beside(paths[host]), "", new_file=True, content=f'__arbor__ = {{"ring": "{ring}"}}\n',
        ))
    return out, skipped


def _host(
    local: Policy, known: Collection[str], paths: Mapping[str, str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> str | None:
    return next((
        name for name in sorted(known)
        if name in paths and not paths[name].endswith("__init__.py") and local.resolve_ring(arbor_index, name) is not None
    ), None)


def _beside(path: str) -> str:
    directory = path.rpartition("/")[0]
    return (directory + "/" if directory else "") + _FILE
