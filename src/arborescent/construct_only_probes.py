"""Selftest probes for ``construct_only`` (#85): one call injected into a module that may not construct the target.

Without a probe, an owner list could be widened or its target mistyped and nothing would notice. Each probe appends a call to the target
(through a plain ``import``, so the spelling needs no new name) to a module the entry does not permit; ``construction-outside-site`` must
newly fire.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import Mutation, Policy, Rule

__all__ = ["construct_only_mutations"]


def construct_only_mutations(
    policy: Policy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons an entry got none). An entry every module may call is reported, never skipped silently."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for item in policy.construct_only:
        host = next((
            name for name in sorted(known)
            if name in paths and not paths[name].endswith("__init__.py") and not item.permits(name, policy.resolve_ring(arbor_index, name))
        ), None)
        if host is None:
            skipped.append(f"construct_only {item.target}: no module outside the owners")
            continue
        out.append(Mutation(
            Rule.CONSTRUCTION_OUTSIDE_SITE, f"construct_only {item.target}: {host} calls it", paths[host],
            f"import {item.target_module}\n_ARBORESCENT_PROBE_CALL = {item.target}()",
        ))
    return out, skipped
