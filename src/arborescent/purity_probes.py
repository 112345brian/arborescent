"""Selftest probes for ``mixed-purity`` (#173): substantial pure functions plus one effect, appended to a module that is not mixed.

The rule is enforced in every ring, so a probe needs no particular ring, only a host that is not already a finding (an
appended block could not then produce a *new* key). Each probe appends ``min_pure_units`` functions of ``min_body_lines``
lines and one effect of a kind the policy counts; ``mixed-purity`` must newly fire for the host. Three shapes, so a
mutation that disables any one path is noticed: an effect inside a function, an effect at import time, and a logging call.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import Mutation, Policy, Rule
from .facts import ModuleInfo
from .purity import Followed, assess, is_allowed, ring_scanned

__all__ = ["mixed_purity_mutations"]


def _pure_block(policy: Policy) -> str:
    rule = policy.mixed_purity
    assignments = "".join(f"    v{i} = x + {i}\n" for i in range(rule.min_body_lines - 1))
    return "".join(f"def _arborescent_probe_pure_{n}(x: int) -> int:\n{assignments}    return v0\n\n\n" for n in range(rule.min_pure_units))


def _effects(kinds: Collection[str]) -> list[tuple[str, str]]:
    """(label, source) for each probe shape the configured kinds allow."""
    out: list[tuple[str, str]] = []
    if "console" in kinds:
        out.append(("a function that prints", "def _arborescent_probe_effect() -> None:\n    print('probe')"))
        out.append(("an import-time print", "print('probe')"))
    elif "filesystem" in kinds:
        out.append(("a function that opens a file", "def _arborescent_probe_effect() -> None:\n    open('probe')"))
    if "logging" in kinds:
        out.append(("a function that logs", "import logging\n\n\ndef _arborescent_probe_log() -> None:\n    logging.info('probe')"))
    return out


def mixed_purity_mutations(
    policy: Policy, paths: Mapping[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
    modules: Mapping[str, ModuleInfo],
) -> tuple[list[Mutation], list[str]]:
    """(mutations, why none). A policy that turns the rule off, or counts no kind a probe can use, is reported, never skipped silently."""
    rule = policy.mixed_purity
    if not rule.enabled:
        return [], ["mixed-purity: disabled by policy"]
    effects = _effects([kind for kind in rule.kinds if kind not in rule.weak_kinds])  # a weak-only host would be a note, not the finding the probe expects
    if not effects:
        return [], ["mixed-purity: no probe shape for the configured `mixed_purity.kinds` less `weak_kinds` (needs console, filesystem or logging)"]
    followed = Followed(modules)
    host = next((
        name for name in sorted(known)
        if name in paths and name in modules and not paths[name].endswith("__init__.py")
        and (ring := policy.resolve_ring(arbor_index, name)) is not None and ring_scanned(policy, ring)
        and not is_allowed(rule, name) and assess(modules[name], policy, followed) is None
    ), None)
    if host is None:
        return [], ["mixed-purity: every module is already mixed, allowed or in a ring the rule skips, so none can be mutated"]
    pure = _pure_block(policy)
    return [
        Mutation(Rule.MIXED_PURITY, f"mixed-purity: {label} beside {rule.min_pure_units} pure functions in {host}", paths[host], pure + source)
        for label, source in effects
    ], []
