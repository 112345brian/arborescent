"""The per-module policy lookup every rule check shares: which architecture scope owns a module, and its resolved ring/annotation state.

Split out of ``rules`` so other rule-check modules (``purity_rules``, ``effect_import_rules``) can depend on it without
creating a cycle back through ``rules`` itself, which is the thing that calls into them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .domain import ArchitecturePolicy, Policy, PolicyError

__all__ = ["RuleContext"]


@dataclass(frozen=True)
class RuleContext:
    policy: Policy
    arbor_index: Mapping[str, tuple[str | None, str | None]]
    scope_by_module: Mapping[str, str]
    tool_modules: frozenset[str] = frozenset()  # modules below a `dev_tools` path: no ring, never imported by product code

    def scope_name(self, name: str) -> str | None:
        return self.scope_by_module.get(name) if self.policy.scopes else None

    def policy_for(self, name: str) -> ArchitecturePolicy | None:
        if not self.policy.scopes:
            return self.policy
        scope_name = self.scope_name(name)
        return self.policy.scope(scope_name).policy if scope_name is not None else None

    def require_policy(self, name: str) -> ArchitecturePolicy:
        policy = self.policy_for(name)
        if policy is None:
            raise PolicyError(f"module {name!r} has no owning architecture scope")
        return policy

    def resolve_ring(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.resolve_ring(self.arbor_index, name) if policy is not None else None

    def annotation_violation(self, name: str) -> str | None:
        policy = self.policy_for(name)
        if policy is not None:
            return policy.annotation_violation(self.arbor_index, name)
        if "arbor-annotations" in self.policy.disabled_tools:
            return None
        annotation = self.arbor_index.get(name)
        return annotation[1] if annotation is not None else None

    def annotation_ring_mismatch(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.annotation_ring_mismatch(self.arbor_index, name) if policy is not None else None

    def annotation_required_violation(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.annotation_required_violation(self.arbor_index, name) if policy is not None else None
