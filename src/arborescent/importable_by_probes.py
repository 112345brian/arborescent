"""Selftest probes for ``importable_by`` (#111): one disallowed import injected per restriction.

Without a probe, an allowlist could be deleted or its key mistyped and nothing would notice. Each probe appends an import of
the restricted module to a module its allowlist does not cover; ``restricted-import`` must newly fire.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping

from .domain import Mutation, Policy, Rule, in_unit

__all__ = ["importable_by_mutations"]


def importable_by_mutations(
    policy: Policy, paths: Mapping[str, str], known: Collection[str]
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons a restriction got none). A restriction with no target or no outside host is reported, never skipped silently."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for pattern, allowed in policy.importable_by:
        target = next((name for name in sorted(known) if in_unit(name, pattern)), None)
        host = next((name for name in sorted(known) if _outsider(name, pattern, allowed, paths)), None)
        if target is None or host is None:
            skipped.append(f"importable_by {pattern}: " + ("no matching module" if target is None else "no module outside the allowlist"))
            continue
        out.append(Mutation(Rule.RESTRICTED_IMPORT, f"importable_by {pattern}: {host} imports {target}", paths[host], f"import {target}"))
    return out, skipped


def _outsider(name: str, pattern: str, allowed: Collection[str], paths: Mapping[str, str]) -> bool:
    return (
        name in paths and not paths[name].endswith("__init__.py")
        and not in_unit(name, pattern) and not any(fnmatch.fnmatchcase(name, glob) for glob in allowed)
    )
