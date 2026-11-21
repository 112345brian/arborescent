"""Query facet for `ArchitecturePolicy`: import/effect/dev-tool rules (which imports and effects a ring or module may use).

Extracted from the policy class's own body so it stays a config-value struct with a self-validating `__post_init__`;
`ArchitecturePolicy`'s (and, for the dev-tool rules, `Policy`'s) methods delegate here unchanged, so no caller's
`policy.forbidden_imports(...)`-style call site moves. The dev-tool rules take their root-only setting directly: `dev_tools`
and `dev_tools_may_import` are whole-run configuration on `Policy`, not per-scope `ArchitectureFields`."""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence

from .policy_rings import ring
from .policy_types import ArchitectureFields, in_unit, path_under
from .presets import DEFAULT_FORBID_CATEGORIES

__all__ = [
    "counts_effect_seam", "forbidden_effects", "forbidden_imports", "forbidden_module_pattern", "forbids_module_import",
    "is_dev_tool_path", "restricted_importers", "restricts_external", "tool_may_import",
]


def counts_effect_seam(policy: ArchitectureFields, seam: int) -> bool:
    """Whether an effect reference with this `EffectUse.seam` kind is a use under `effect_defaults`."""
    if policy.effect_defaults == "count":
        return True
    return seam == 0 if policy.effect_defaults == "injection" else seam != 1


def tool_may_import(dev_tools_may_import: Sequence[str], target: str) -> bool:
    return not dev_tools_may_import or any(fnmatch.fnmatchcase(target, pattern) for pattern in dev_tools_may_import)


def is_dev_tool_path(dev_tools: Sequence[str], rel: str) -> bool:
    """Whether a repository-relative path is, or lies below, a `dev_tools` entry."""
    return any(path_under(rel, entry) for entry in dev_tools)


def forbidden_module_pattern(policy: ArchitectureFields, module: str) -> str | None:
    """The first `forbidden_modules` glob matching the module's full dotted name, or None."""
    return next((p for p in policy.forbidden_modules if fnmatch.fnmatchcase(module, p)), None)


def forbidden_imports(policy: ArchitectureFields, module: str) -> frozenset[str]:
    """Module-specific internal import restrictions, additive across matching globs."""
    return frozenset(target for pattern, targets in policy.module_forbid_imports if fnmatch.fnmatchcase(module, pattern) for target in targets)


def restricted_importers(policy: ArchitectureFields, importer: str, target: str) -> tuple[str, ...] | None:
    """The importer globs that forbid ``importer`` from importing ``target`` under ``importable_by``, or None when it is allowed.

    A module inside a restricted unit (the module itself or its submodules) may always import within it."""
    for pattern, allowed in policy.importable_by:
        if in_unit(target, pattern) and not in_unit(importer, pattern) and not any(fnmatch.fnmatchcase(importer, glob) for glob in allowed):
            return allowed
    return None


def forbids_module_import(policy: ArchitectureFields, module: str, target: str) -> str | None:
    """Return the configured target restriction that matches an imported module, if any."""
    banned_targets = forbidden_imports(policy, module)
    return next((banned for banned in sorted(banned_targets) if target == banned or target.startswith(banned + ".") or fnmatch.fnmatchcase(target, banned)), None)


def forbidden_effects(policy: ArchitectureFields, ring_name: str) -> frozenset[str]:
    """Resolve a ring's effect policy; the outermost ring wins for a one-ring policy."""
    rule = ring(policy, ring_name)
    position = next(i for i, candidate in enumerate(policy.rings) if candidate.name == ring_name)
    forbidden: frozenset[str]
    if rule.forbid is not None:
        forbidden = rule.forbid
    elif position == len(policy.rings) - 1:
        forbidden = frozenset[str]()
    elif position == 0:
        forbidden = DEFAULT_FORBID_CATEGORIES
    else:
        forbidden = policy.preset.middle_forbid
    if forbidden and "dynamic-import" not in forbidden:
        forbidden = forbidden | frozenset[str]({"dynamic-import"})
    return forbidden


def restricts_external(policy: ArchitectureFields, top: str, is_stdlib: bool) -> bool:
    """Whether importing external package ``top`` needs an entry in the importing ring's allow-list."""
    return policy.preset.restricts(top, is_stdlib, policy.io_modules)
