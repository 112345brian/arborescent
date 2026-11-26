"""Selftest probes for ``importable_by`` (#111): one disallowed import injected per restriction.

Without a probe, an allowlist could be deleted or its key mistyped and nothing would notice. Each probe appends an import of
the restricted module to a module its allowlist does not cover; ``restricted-import`` must newly fire.

``private_access`` (#218), the other "who may reach this" rule, is probed here too: one private attribute of another module.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping

from .domain import ArchitecturePolicy, Mutation, Rule, in_unit

__all__ = ["importable_by_mutations", "private_access_mutations"]


def importable_by_mutations(
    policy: ArchitecturePolicy, paths: Mapping[str, str], known: Collection[str]
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


def private_access_mutations(
    policy: ArchitecturePolicy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """One checked module reads a private name of a module it is not inside; nothing when ``private_access`` is off."""
    setting = policy.private_access
    if setting is None:
        return [], []
    for host in sorted(known):
        if host not in paths or paths[host].endswith("__init__.py") or any(in_unit(host, glob) for glob in setting.exempt):
            continue
        if setting.rings and policy.resolve_ring(arbor_index, host) not in setting.rings:
            continue
        target = next((
            name for name in sorted(known)
            if host != name and not host.startswith(name + ".")
            and not any(in_unit(f"{name}._arborescent_probe_private", glob) for glob in setting.allow)
        ), None)
        if target is None:
            continue
        probe = f"import {target} as _arborescent_probe_module\n_ARBORESCENT_PROBE_PRIVATE = _arborescent_probe_module._arborescent_probe_private"
        return [Mutation(Rule.PRIVATE_ACCESS, f"private_access: {host} reads a private name of {target}", paths[host], probe)], []
    return [], [f"{Rule.PRIVATE_ACCESS}: no checked module with another module to reach into"]
