"""Selftest probes for ``owned_literal`` (#112): one spelling injected into a module that does not own the literal.

Without a probe, an owner list could be widened or its literal mistyped and nothing would notice. Each probe appends the
literal as a string constant to a module the entry does not permit; ``literal-outside-owner`` must newly fire.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import Mutation, Policy, Rule

__all__ = ["owned_literal_mutations"]


def owned_literal_mutations(
    policy: Policy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons an entry got none). An entry every module is allowed to spell is reported, never skipped silently."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for item in policy.owned_literals:
        host = next((
            name for name in sorted(known)
            if name in paths and not paths[name].endswith("__init__.py") and not item.permits(name, policy.resolve_ring(arbor_index, name))
        ), None)
        if host is None:
            skipped.append(f"owned_literal {item.literal}: no module outside the owners")
            continue
        out.append(Mutation(
            Rule.LITERAL_OUTSIDE_OWNER, f"owned_literal {item.literal}: {host} spells it", paths[host],
            f"_ARBORESCENT_PROBE_LITERAL = {item.literal!r}",
        ))
    return out, skipped
