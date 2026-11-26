"""Selftest probes for the call rules, ``construct_only`` (#85), ``call_allowlist`` (#229) and ``companion`` (#231): calls injected where they are not allowed.

Without a probe, an owner list could be widened or its target mistyped and nothing would notice. Each probe appends a call to the target
(through a plain ``import``, so the spelling needs no new name) to a module the entry does not permit; ``construction-outside-site`` must
newly fire. A ``call_allowlist`` entry gets the same: the first of its ``calls``, or for an idiom a function making one call of each half
(a ``*`` is filled with a probe name). A ``companion`` entry gets a function, in a module the entry watches, that does the action and
records nothing.

Each ``[[construction]]`` entry (#234) and each field marked ``metadata={"arbor": "derived"}`` is probed the same way: a
call that passes the forbidden keyword, one that leaves out the required ones, and one from outside ``only_from``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .definition_facts import ClassIndex, IndexedModule
from .domain import (
    ArchitecturePolicy,
    CallAllowance,
    ConstructionSite,
    Mutation,
    Rule,
)

__all__ = ["call_allowlist_mutations", "companion_mutations", "construct_only_mutations", "construction_mutations"]


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
        if parent == "pathlib.Path":  # a method on a Path value, which is how the call is usually spelled
            imports.append(f"import pathlib as _arborescent_probe_{position}")
            calls.append(f'_arborescent_probe_{position}.Path("_arborescent_probe").{function}("_arborescent_probe")')
        else:
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


def construction_mutations(
    policy: ArchitecturePolicy, paths: Mapping[str, str], arbor_index: Mapping[str, tuple[str | None, str | None]],
    modules: Mapping[str, IndexedModule],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, reasons a restriction got none) for ``[[construction]]`` entries and derived-field markers.

    ``paths`` is this policy's (or scope's) modules. The argument probes add a module of their own, so a call already
    recorded in the baseline cannot hide them."""
    hosts = [name for name in sorted(paths) if not paths[name].endswith("__init__.py")]
    classes = ClassIndex(modules)
    marked = [
        (target, field) for target, (module, _) in sorted(classes.classes.items()) if module in paths
        for field in sorted(classes.signature(target).derived)
    ]
    forbidden = marked + [(item.target, field) for item in policy.construction for field in item.forbid_kwargs]
    out: list[Mutation] = []
    skipped: list[str] = []
    for target, field in dict.fromkeys(forbidden):
        label = f"construction {target}: passes `{field}`"
        if not hosts:
            skipped.append(f"{label}: no module to call it from")
            continue
        out.append(_new_module(Rule.CONSTRUCTION_FORBIDDEN_ARGUMENT, label, paths[hosts[0]], target, f"{field}=None"))
    for item in policy.construction:
        if item.require_kwargs and hosts:
            label = f"construction {item.target}: leaves out {list(item.require_kwargs)}"
            out.append(_new_module(Rule.CONSTRUCTION_MISSING_ARGUMENT, label, paths[hosts[0]], item.target, ""))
        if not item.modules:
            continue
        host = _outsider(item, policy, hosts, arbor_index)
        if host is None:
            skipped.append(f"construction {item.target}: no module outside `only_from`")
        else:
            label = f"construction {item.target}: {host} constructs it"
            out.append(Mutation(Rule.CONSTRUCTION_OUTSIDE_SITE, label, paths[host], _call(item.target, "")))
    return out, skipped


def _outsider(
    item: ConstructionSite, policy: ArchitecturePolicy, hosts: list[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> str | None:
    return next((name for name in hosts if not item.permits(name, policy.resolve_ring(arbor_index, name))), None)


def _call(target: str, arguments: str) -> str:
    return f"import {target.rpartition('.')[0]}\n_ARBORESCENT_PROBE_CALL = {target}({arguments})"


def _new_module(rule: Rule, label: str, beside: str, target: str, arguments: str) -> Mutation:
    folder = beside.rpartition("/")[0]
    path = (folder + "/" if folder else "") + "arborescent_probe_construction.py"
    return Mutation(rule, label, path, "", new_file=True, content=_call(target, arguments) + "\n")
