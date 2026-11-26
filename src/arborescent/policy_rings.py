"""Query facet for `ArchitecturePolicy`: ring lookup and `__arbor__` annotation resolution.

Extracted from the policy class's own body so it stays a config-value struct with a self-validating `__post_init__`;
`ArchitecturePolicy`'s methods delegate here unchanged, so no caller's `policy.ring_of(...)`-style call site moves. Every
function reads only `ArchitectureFields`, the per-scope rule data, so it answers for a scope's policy and the root alike;
scope lookup (which needs the root's `scopes`) lives in `architecture_policy`."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping

from .policy_types import ALL, ArchitectureFields, PolicyError, RingRule

__all__ = [
    "allowed_rings", "annotation_required_violation", "annotation_ring_mismatch", "annotation_violation",
    "expressible_by_layers", "resolve_ring", "ring", "ring_of", "unclassified_detail",
]


def ring(policy: ArchitectureFields, name: str) -> RingRule:
    for r in policy.rings:
        if r.name == name:
            return r
    raise PolicyError(f"unknown ring {name!r}")


def ring_of(policy: ArchitectureFields, module: str) -> str | None:
    for pattern, r in policy.classify:
        if fnmatch.fnmatchcase(module, pattern):
            return r
    return None


def unclassified_detail(policy: ArchitectureFields, module: str) -> str:
    """Explain a missing ring, hinting at ``pkg*`` when a `classify` glob names only a parent package."""
    detail = "module matches no [classify] pattern; every module needs a ring"
    parts = module.split(".")
    parents = {".".join(parts[:end]) for end in range(1, len(parts))}
    hint = next((pattern for pattern, _ in policy.classify if pattern in parents), None)
    if hint is None:
        return detail
    return f"{detail}; `classify` matches whole module names, so {hint!r} does not cover submodules (did you mean {hint + '*'!r}?)"


def _has_valid_annotation(policy: ArchitectureFields, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> bool:
    if "arbor-annotations" in policy.disabled_tools:
        return False
    annotation = arbor_index.get(name)
    if annotation is None:
        return False
    r, violation = annotation
    return violation is None and r is not None and any(rule.name == r for rule in policy.rings)


def resolve_ring(policy: ArchitectureFields, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
    """Resolve a module's annotated ring, falling back to ``classify`` when unavailable or invalid."""
    if _has_valid_annotation(policy, arbor_index, name):
        r, _ = arbor_index[name]
        return r
    return ring_of(policy, name)


def annotation_violation(policy: ArchitectureFields, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
    """Return why a present annotation is invalid, independently of ring fallback."""
    if "arbor-annotations" in policy.disabled_tools:
        return None
    annotation = arbor_index.get(name)
    if annotation is None:
        return None
    r, violation = annotation
    if violation is not None:
        return violation
    if r is not None and not any(rule.name == r for rule in policy.rings):
        return f"__arbor__ names unknown ring {r!r}"
    return None


def annotation_ring_mismatch(policy: ArchitectureFields, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
    """Return why a valid annotation's ring disagrees with what ``classify`` would assign, if it does."""
    if "arbor-annotations" in policy.disabled_tools or annotation_violation(policy, arbor_index, name) is not None:
        return None
    annotation = arbor_index.get(name)
    if annotation is None:
        return None
    r, _ = annotation
    if r is None:
        return None
    classify_ring = ring_of(policy, name)
    if classify_ring is not None and classify_ring != r:
        return f"__arbor__ names ring {r!r}, but `classify` would assign {classify_ring!r}"
    return None


def annotation_required_violation(policy: ArchitectureFields, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
    """Return why a module needs, but lacks, a valid `__arbor__` annotation, under `require_arbor_annotations`."""
    if not policy.require_arbor_annotations:
        return None
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in policy.require_arbor_annotations_exclude):
        return None
    if _has_valid_annotation(policy, arbor_index, name):
        return None
    r = ring_of(policy, name)
    if r is None:
        return None  # unclassified's job, not this rule's
    return f"module has no __arbor__ annotation; `classify` assigns {r!r} only as a legacy fallback; place a new module with `arborescent admit {name} --ring R`, or migrate a legacy one with `arborescent annotate --legacy --against REF`"


def allowed_rings(policy: ArchitectureFields, ring_name: str) -> frozenset[str]:
    """The rings a module of ``ring_name`` may import: itself plus its declared inner rings."""
    rule = ring(policy, ring_name)
    names = [r.name for r in policy.rings]
    inner = names[: names.index(ring_name)]
    return frozenset({ring_name, *(inner if ALL in rule.may_import else rule.may_import & set(inner))})


def expressible_by_layers(policy: ArchitectureFields) -> bool:
    """True when every ring may import ALL inner rings, so tach's layers alone state the policy."""
    return all(allowed_rings(policy, r.name) == frozenset(x.name for x in policy.rings[: i + 1]) for i, r in enumerate(policy.rings))
