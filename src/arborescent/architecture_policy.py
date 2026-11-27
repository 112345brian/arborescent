"""`ArchitecturePolicy`: what one module or ring obeys, the part of the policy an `ArchitectureScope` holds for itself.

`domain.Policy` extends it with the whole-run tool configuration (python, type checkers, baseline, ratchets, packages,
children, ...) that exists once per `arborescent check`, so the root policy of an unscoped repository is still accepted
wherever a scope's policy is. Its query methods delegate to the `policy_rings`/`policy_imports` facets, which read only the
pure-data `policy_types.ArchitectureFields` base, so no module here imports one that imports it back."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields

from . import policy_imports, policy_rings
from .effect_categories import EFFECT_CATEGORIES
from .policy_types import ALL, ArchitectureFields, PolicyError, RingRule

__all__ = ["DISABLED_TOOL_NAMES", "ArchitecturePolicy", "ArchitectureScope", "architecture_of", "validate_architecture"]

DISABLED_TOOL_NAMES = frozenset({"import-linter", "tach", "ruff", "pyright", "basedpyright", "ty", "pyrefly", "arbor-annotations"})

ArborIndex = Mapping[str, tuple[str | None, str | None]]


@dataclass(frozen=True)
class ArchitecturePolicy(ArchitectureFields):
    """Per-module/per-ring rule resolution: the rings, their classification and the import/effect/annotation rules.

    A scope's policy is exactly this; `domain.Policy` (the root) is this plus whole-run configuration."""

    def __post_init__(self) -> None:
        validate_architecture(self, has_scopes=False)

    # Ring/annotation lookup lives in `policy_rings`; import/effect rules in `policy_imports`. Both are pure query facets
    # over this struct's fields: delegation only, so no caller's call site moves.

    def ring(self, name: str) -> RingRule:
        return policy_rings.ring(self, name)

    def ring_of(self, module: str) -> str | None:
        return policy_rings.ring_of(self, module)

    def counts_effect_seam(self, seam: int) -> bool:
        """Whether an effect reference with this `EffectUse.seam` kind is a use under `effect_defaults`."""
        return policy_imports.counts_effect_seam(self, seam)

    def forbidden_module_pattern(self, module: str) -> str | None:
        """The first `forbidden_modules` glob matching the module's full dotted name, or None."""
        return policy_imports.forbidden_module_pattern(self, module)

    def unclassified_detail(self, module: str) -> str:
        """Explain a missing ring, hinting at ``pkg*`` when a `classify` glob names only a parent package."""
        return policy_rings.unclassified_detail(self, module)

    def resolve_ring(self, arbor_index: ArborIndex, name: str) -> str | None:
        """Resolve a module's annotated ring, falling back to ``classify`` when unavailable or invalid."""
        return policy_rings.resolve_ring(self, arbor_index, name)

    def annotation_violation(self, arbor_index: ArborIndex, name: str) -> str | None:
        """Return why a present annotation is invalid, independently of ring fallback."""
        return policy_rings.annotation_violation(self, arbor_index, name)

    def annotation_ring_mismatch(self, arbor_index: ArborIndex, name: str) -> str | None:
        """Return why a valid annotation's ring disagrees with what ``classify`` would assign, if it does."""
        return policy_rings.annotation_ring_mismatch(self, arbor_index, name)

    def annotation_required_violation(self, arbor_index: ArborIndex, name: str) -> str | None:
        """Return why a module needs, but lacks, a valid `__arbor__` annotation, under `require_arbor_annotations`."""
        return policy_rings.annotation_required_violation(self, arbor_index, name)

    def forbidden_imports(self, module: str) -> frozenset[str]:
        """Module-specific internal import restrictions, additive across matching globs."""
        return policy_imports.forbidden_imports(self, module)

    def restricted_importers(self, importer: str, target: str) -> tuple[str, ...] | None:
        """The importer globs that forbid ``importer`` from importing ``target`` under ``importable_by``, or None when it is allowed.

        A module inside a restricted unit (the module itself or its submodules) may always import within it."""
        return policy_imports.restricted_importers(self, importer, target)

    def forbids_module_import(self, module: str, target: str) -> str | None:
        """Return the configured target restriction that matches an imported module, if any."""
        return policy_imports.forbids_module_import(self, module, target)

    def allowed_rings(self, ring: str) -> frozenset[str]:
        """The rings a module of ``ring`` may import: itself plus its declared inner rings."""
        return policy_rings.allowed_rings(self, ring)

    def forbidden_effects(self, ring: str) -> frozenset[str]:
        """Resolve a ring's effect policy; the outermost ring wins for a one-ring policy."""
        return policy_imports.forbidden_effects(self, ring)

    def restricts_external(self, top: str, is_stdlib: bool) -> bool:
        """Whether importing external package ``top`` needs an entry in the importing ring's allow-list."""
        return policy_imports.restricts_external(self, top, is_stdlib)

    def expressible_by_layers(self) -> bool:
        """True when every ring may import ALL inner rings, so tach's layers alone state the policy."""
        return policy_rings.expressible_by_layers(self)


@dataclass(frozen=True)
class ArchitectureScope:
    """One independently governed source tree and its import boundary."""

    name: str
    policy: ArchitecturePolicy
    public: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


def architecture_of(policy: ArchitectureFields) -> ArchitecturePolicy:
    """The `ArchitecturePolicy` part of a (root or scope) policy: its rule data, without any whole-run configuration."""
    if type(policy) is ArchitecturePolicy:
        return policy
    return ArchitecturePolicy(**{f.name: getattr(policy, f.name) for f in fields(ArchitectureFields)})


def validate_architecture(policy: ArchitectureFields, has_scopes: bool) -> None:
    """The invariants of the rule data alone; a scoped root (``has_scopes``) may have no rings of its own."""
    _validate_rings(policy.rings, policy.classify, has_scopes)
    _validate_pattern_table("module_forbid_imports", policy.module_forbid_imports, "target")
    _validate_pattern_table("importable_by", policy.importable_by, "importer")
    _validate_disabled_tools(policy.disabled_tools)
    checked = () if policy.private_access is None else policy.private_access.rings
    unknown = set(checked) - {rule.name for rule in policy.rings}
    if unknown and policy.rings:  # a scoped root has no rings of its own; each scope validates its table against its own
        raise PolicyError(f"`private_access` rings name unknown rings: {sorted(unknown)}")
    if policy.require_arbor_annotations and "arbor-annotations" in policy.disabled_tools:
        raise PolicyError(
            "`require_arbor_annotations` cannot be combined with `disabled_tools = [\"arbor-annotations\"]`"
        )


def _validate_disabled_tools(tools: tuple[str, ...]) -> None:
    unknown = set(tools) - DISABLED_TOOL_NAMES
    if unknown:
        raise PolicyError(f"`disabled_tools` contains unknown tool names: {sorted(unknown)}")
    if len(set(tools)) != len(tools):
        raise PolicyError("`disabled_tools` must not contain duplicate names")


def _validate_pattern_table(setting: str, table: Sequence[tuple[str, tuple[str, ...]]], what: str) -> None:
    for pattern, names in table:
        if not pattern or not names:
            raise PolicyError(f"`{setting}` needs non-empty module patterns and {what} lists")


def _validate_rings(rings: tuple[RingRule, ...], classify: tuple[tuple[str, str], ...], has_scopes: bool) -> None:
    names = [rule.name for rule in rings]
    if (not names and not has_scopes) or len(set(names)) != len(names):
        raise PolicyError("`rings` must list distinct ring names, innermost first")
    for index, rule in enumerate(rings):
        invalid = rule.may_import - {ALL, *names[:index]}
        if invalid:
            raise PolicyError(
                f"ring {rule.name!r} may only import inner rings {names[:index]}, not {sorted(invalid)} (onion rule)"
            )
        if rule.forbid is not None and (unknown := rule.forbid - EFFECT_CATEGORIES):
            raise PolicyError(f"ring {rule.name!r} forbids unknown effect categories {sorted(unknown)}")
        if unknown := rule.forbid_at_import - EFFECT_CATEGORIES:
            raise PolicyError(f"ring {rule.name!r} forbids unknown effect categories at import {sorted(unknown)}")
    for pattern, ring in classify:
        if ring not in names:
            raise PolicyError(f"classify {pattern!r} names unknown ring {ring!r}")
