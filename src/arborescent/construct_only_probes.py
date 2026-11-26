"""Selftest probes for the call rules, ``construct_only`` (#85), ``call_allowlist`` (#229) and ``companion`` (#231): calls injected where they are not allowed.

Without a probe, an owner list could be widened or its target mistyped and nothing would notice. Each probe appends a call to the target
(through a plain ``import``, so the spelling needs no new name) to a module the entry does not permit; ``construction-outside-site`` must
newly fire. A ``call_allowlist`` entry gets the same: the first of its ``calls``, or for an idiom a function making one call of each half
(a ``*`` is filled with a probe name). A ``companion`` entry gets a function, in a module the entry watches, that does the action and
records nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import ArchitecturePolicy, CallAllowance, Mutation, Rule

__all__ = ["call_allowlist_mutations", "companion_mutations", "construct_only_mutations"]


def construct_only_mutations(
    policy: ArchitecturePolicy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
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


def call_allowlist_mutations(
    policy: ArchitecturePolicy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons an entry got none), as above."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for item in policy.call_allowlist:
        label = f"call_allowlist {', '.join(item.calls)}"
        host = next((
            name for name in sorted(known)
            if name in paths and not paths[name].endswith("__init__.py") and not item.permits(name, policy.resolve_ring(arbor_index, name))
        ), None)
        if host is None:
            skipped.append(f"{label}: no module outside the owners")
            continue
        out.append(Mutation(
            Rule.HAND_ROLLED_IDIOM if item.and_calls else Rule.CALL_OUTSIDE_ALLOWLIST, f"{label}: {host} does it", paths[host],
            _probe_source(item),
        ))
    return out, skipped


def _probe_source(item: CallAllowance) -> str:
    """Import each target's parent under a private name, so the probe never rebinds a name the host already uses."""
    imports: list[str] = []
    calls: list[str] = []
    for position, pattern in enumerate((item.calls[0], *item.and_calls[:1])):
        parent, _, function = pattern.replace("*", "_arborescent_probe").rpartition(".")
        imports.append(f"import {parent} as _arborescent_probe_{position}")
        calls.append(f"_arborescent_probe_{position}.{function}()")
    if item.and_calls:
        calls = ["def _arborescent_probe_idiom() -> None:", *(f"    {call}" for call in calls)]
    return "\n".join(imports + calls)


def companion_mutations(
    policy: ArchitecturePolicy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons an entry got none), as above. A survivor can mean the action itself records, so the rule never fires."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for item in policy.companion:
        label = f"companion {', '.join(item.when_calls)}"
        host = next((
            name for name in sorted(known)
            if name in paths and not paths[name].endswith("__init__.py") and item.applies_to(name, policy.resolve_ring(arbor_index, name))
        ), None)
        if host is None:
            skipped.append(f"{label}: no module in scope")
            continue
        parent, _, function = item.when_calls[0].replace("*", "_arborescent_probe").rpartition(".")
        if item.when_calls[0].startswith("*."):  # a method on a value: any receiver will do
            source = f"def _arborescent_probe_companion(_arborescent_probe_value: object) -> None:\n    _arborescent_probe_value.{function}()"
        else:
            source = f"import {parent} as _arborescent_probe_0\ndef _arborescent_probe_companion() -> None:\n    _arborescent_probe_0.{function}()"
        out.append(Mutation(Rule.MISSING_COMPANION_CALL, f"{label}: {host} does it and records nothing", paths[host], source))
    return out, skipped
