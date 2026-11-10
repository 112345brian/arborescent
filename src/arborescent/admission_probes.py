"""``selftest`` probes for ``arborescent admit`` (#161): synthetic modules the gate must refuse and admit.

Admission is not a rule ``check`` reports, so the file-mutation probes cannot kill it; these call ``admit_check`` directly on facts
extracted from small sources, against the policy's own rings.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .classify_audit import admit_check
from .domain import Policy
from .facts import ModuleInfo, extract
from .rules import RuleContext

__all__ = ["admission_probes"]


def _info(name: str, source: str) -> ModuleInfo:
    return ModuleInfo(name, f"{name.replace('.', '/')}.py", False, extract(source))


def admission_probes(
    policy: Policy, arbor_index: Mapping[str, tuple[str | None, str | None]], scope_by_module: Mapping[str, str], known: Collection[str],
) -> tuple[list[str], list[str], list[str]]:
    """``(killed, survived, skipped)`` labels. A probe is killed when the gate gives the answer the design requires."""
    if policy.scopes or len(policy.rings) < 2:
        return [], [], ["admission: probes need a single-scope policy with at least two rings"]
    context = RuleContext(policy, arbor_index, scope_by_module)
    inner, outer = policy.rings[0].name, policy.rings[-1].name
    anchor = next((m for m in sorted(known) if context.resolve_ring(m) == outer), None)
    cases = [
        ("an empty module is admitted to the innermost ring", _info("probe.empty", ""), inner, None, True),
        ("an empty module is refused the outer ring when a tighter one fits", _info("probe.empty", ""), outer, None, False),
        ("a recorded reason excuses the outer ring", _info("probe.empty", ""), outer, "selftest probe", True),
        ("a blank reason excuses nothing", _info("probe.empty", ""), outer, "  ", False),
        ("a non-literal dynamic import is refused without a reason", _info("probe.dyn", "import importlib\nimportlib.import_module(name)\n"), outer, None, False),
    ]
    if anchor is not None:
        lazy = f"def f() -> None:\n    import {anchor}\n"
        cases.append(("a function-local import of an outer-ring module is refused for the innermost ring", _info("probe.lazy", lazy), inner, None, False))
    killed: list[str] = []
    survived: list[str] = []
    for label, info, ring, reason, expected in cases:
        outcome = admit_check(context, info.name, info, ring, known, reason)
        (killed if outcome.admitted == expected else survived).append(f"[admission] {label}")
    return killed, survived, []
