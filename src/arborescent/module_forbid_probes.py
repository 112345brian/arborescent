"""Selftest probes for ``module_forbid_imports`` (#67): one forbidden import injected per configured pattern.

Without a probe, a policy line such as "the annotation layer must not import the driver" could be deleted or mistyped and
nothing would notice. Each probe appends the forbidden import to a module that matches the pattern; ``internal-import``
must newly fire.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping

from .domain import Policy, Rule
from .mutation import Mutation

__all__ = ["module_forbid_mutations"]


def module_forbid_mutations(
    policy: Policy, paths: Mapping[str, str], known: Collection[str]
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons a pattern got none). A pattern with no host or no target module is reported, never skipped silently."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for pattern, targets in policy.module_forbid_imports:
        host = next((name for name in sorted(known) if name in paths and not paths[name].endswith("__init__.py") and fnmatch.fnmatchcase(name, pattern)), None)
        target = next((name for name in targets if name in known), None)
        if host is None or target is None:
            skipped.append(f"module_forbid_imports {pattern}: no matching module" if host is None else f"module_forbid_imports {pattern}: no forbidden target is a known module")
            continue
        out.append(Mutation(Rule.INTERNAL_IMPORT, f"module_forbid_imports {pattern}: forbidden import of {target}", paths[host], f"import {target}"))
    return out, skipped
