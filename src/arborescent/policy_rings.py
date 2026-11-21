"""Query facet for `Policy`: ring/scope lookup and `__arbor__` annotation resolution (#?)

Extracted from `Policy`'s own body so the class stays a config-value struct with a self-validating `__post_init__`;
`Policy`'s methods delegate here unchanged, so no caller's `policy.ring_of(...)`-style call site moves."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping

from .policy_types import (
    ALL,
    ArchitectureScope,
    PolicyError,
    PolicyLike,
    RingRule,
    normalized_source_path,
    path_contains,
)

__all__ = [
    "allowed_rings", "annotation_required_violation", "annotation_ring_mismatch", "annotation_violation",
    "expressible_by_layers", "resolve_ring", "resolve_scope_ring", "ring", "ring_of", "scope",
    "scope_annotation_required_violation", "scope_annotation_violation", "scope_for_path", "unclassified_detail",
]


def ring(policy: PolicyLike, name: str) -> RingRule:
    for r in policy.rings:
        if r.name == name:
            return r
    raise PolicyError(f"unknown ring {name!r}")


def scope(policy: PolicyLike, name: str) -> ArchitectureScope:
    """Return a named scope or fail with a policy error."""
    for s in policy.scopes:
        if s.name == name:
            return s
    raise PolicyError(f"unknown scope {name!r}")


def scope_for_path(policy: PolicyLike, path: str) -> ArchitectureScope | None:
    """Find the scope whose source path owns a repository-relative file path."""
    normalized = normalized_source_path(path)
    for s in policy.scopes:
        if any(path_contains(source, normalized) for source in s.policy.sources):
            return s
    return None


def resolve_scope_ring(
    policy: PolicyLike, scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str,
) -> str | None:
    """Resolve annotations against only the named scope's ring set."""
    return scope(policy, scope_name).policy.resolve_ring(arbor_index, module)


def scope_annotation_violation(
    policy: PolicyLike, scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str,
) -> str | None:
    """Validate a module annotation against only its owning scope's rings."""
    return scope(policy, scope_name).policy.annotation_violation(arbor_index, module)


def scope_annotation_required_violation(
    policy: PolicyLike, scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str,
) -> str | None:
    """Validate `require_arbor_annotations` against only its owning scope's policy."""
    return scope(policy, scope_name).policy.annotation_required_violation(arbor_index, module)


def ring_of(policy: PolicyLike, module: str) -> str | None:
    for pattern, r in policy.classify:
        if fnmatch.fnmatchcase(module, pattern):
            return r
    return None


def unclassified_detail(policy: PolicyLike, module: str) -> str:
    """Explain a missing ring, hinting at ``pkg*`` when a `classify` glob names only a parent package."""
    detail = "module matches no [classify] pattern; every module needs a ring"
    parts = module.split(".")
    parents = {".".join(parts[:end]) for end in range(1, len(parts))}
    hint = next((pattern for pattern, _ in policy.classify if pattern in parents), None)
    if hint is None:
        return detail
    return f"{detail}; `classify` matches whole module names, so {hint!r} does not cover submodules (did you mean {hint + '*'!r}?)"


def _has_valid_annotation(policy: PolicyLike, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> bool:
    if "arbor-annotations" in policy.disabled_tools:
        return False
    annotation = arbor_index.get(name)
    if annotation is None:
        return False
    r, violation = annotation
    return violation is None and r is not None and any(rule.name == r for rule in policy.rings)


def resolve_ring(policy: PolicyLike, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
    """Resolve a module's annotated ring, falling back to ``classify`` when unavailable or invalid."""
    if _has_valid_annotation(policy, arbor_index, name):
        r, _ = arbor_index[name]
        return r
    return ring_of(policy, name)


def annotation_violation(policy: PolicyLike, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
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


def annotation_ring_mismatch(policy: PolicyLike, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
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


def annotation_required_violation(policy: PolicyLike, arbor_index: Mapping[str, tuple[str | None, str | None]], name: str) -> str | None:
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


def allowed_rings(policy: PolicyLike, ring_name: str) -> frozenset[str]:
    """The rings a module of ``ring_name`` may import: itself plus its declared inner rings."""
    rule = ring(policy, ring_name)
    names = [r.name for r in policy.rings]
    inner = names[: names.index(ring_name)]
    return frozenset({ring_name, *(inner if ALL in rule.may_import else rule.may_import & set(inner))})


def expressible_by_layers(policy: PolicyLike) -> bool:
    """True when every ring may import ALL inner rings, so tach's layers alone state the policy."""
    return all(allowed_rings(policy, r.name) == frozenset(x.name for x in policy.rings[: i + 1]) for i, r in enumerate(policy.rings))
