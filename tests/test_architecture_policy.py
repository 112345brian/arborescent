"""`ArchitecturePolicy`: the per-scope rule data a scope holds, split from `Policy`'s whole-run tool configuration.

The behavioral tests pin what every `scope.policy.*` caller relied on while a scope still held a full nested `Policy`;
the shape tests pin the split itself (a scope holds the narrow type, and only `config._LEGACY_ARCHITECTURE_KEYS`'s
rule data plus the inherited `disabled_tools` live on it)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from arborescent import config
from arborescent.domain import ArchitecturePolicy, Policy, PolicyError, RingRule


def _scoped(**root: object) -> Policy:
    raw: dict[str, object] = {
        "scopes": {
            "alpha": {
                "sources": ["alpha"],
                "source_roots": ["."],
                "rings": ["domain", "adapters"],
                "ring": {"adapters": {"may_import": ["domain"], "external": ["*"]}},
                "classify": {"alpha.core*": "domain", "alpha.io*": "adapters"},
                "entry_scripts": ["alpha.tool"],
                "dependencies": {"yaml": "PyYAML"},
                "forbid_everywhere": ["pickle"],
                "effect_defaults": "count",
            },
            "beta": {
                "sources": ["beta"],
                "source_roots": ["."],
                "rings": ["core"],
                "classify": {"beta.*": "core"},
                "forbid_circular_imports": False,
                "require_arbor_annotations": True,
            },
        },
        **root,
    }
    return config.policy_from_mapping(raw)


def test_each_scope_resolves_rules_against_only_its_own_rings() -> None:
    policy = _scoped()
    alpha, beta = policy.scope("alpha").policy, policy.scope("beta").policy
    assert alpha.ring_of("alpha.core.model") == "domain"
    assert alpha.ring_of("beta.anything") is None
    assert beta.ring_of("beta.anything") == "core"
    assert alpha.allowed_rings("adapters") == {"adapters", "domain"}
    assert "dynamic-import" in alpha.forbidden_effects("domain")
    assert alpha.counts_effect_seam(1) and not beta.counts_effect_seam(1)
    assert alpha.entry_scripts == ("alpha.tool",) and dict(alpha.dependencies) == {"yaml": "PyYAML"}
    assert alpha.forbid_everywhere == ("pickle",) and beta.forbid_everywhere == ()
    assert alpha.forbid_circular_imports and not beta.forbid_circular_imports
    with pytest.raises(PolicyError, match="unknown ring"):
        alpha.ring("core")


def test_scope_annotations_resolve_per_scope_and_honour_the_root_disabled_tools() -> None:
    index = {"beta.x": ("core", None), "alpha.core.m": ("adapters", None)}
    policy = _scoped()
    assert policy.resolve_scope_ring("beta", index, "beta.x") == "core"
    assert policy.scope_annotation_violation("alpha", {"alpha.core.m": ("core", None)}, "alpha.core.m") is not None
    assert policy.scope("alpha").policy.resolve_ring(index, "alpha.core.m") == "adapters"
    assert policy.scope_annotation_required_violation("beta", {}, "beta.y") is not None
    disabled = _scoped(disabled_tools=["arbor-annotations"], scopes={
        "alpha": {"sources": ["alpha"], "source_roots": ["."], "rings": ["domain", "adapters"], "classify": {"alpha.core*": "domain"}},
    })
    assert disabled.scope("alpha").policy.resolve_ring(index, "alpha.core.m") == "domain"  # annotation ignored, classify wins
    assert disabled.scope("alpha").policy.annotation_violation({"alpha.core.m": ("nowhere", None)}, "alpha.core.m") is None


def test_root_unions_the_scopes_sources_and_entry_scripts() -> None:
    policy = _scoped()
    assert policy.rings == () and policy.classify == ()
    assert policy.sources == ("alpha", "beta")
    assert policy.entry_scripts == ("alpha.tool",)
    assert policy.scope_for_path("beta/x.py") is policy.scope("beta")
    assert policy.scope_for_path("gamma/x.py") is None


def test_a_scope_holds_an_architecture_policy_not_a_nested_run_configuration() -> None:
    policy = _scoped(python="3.12", type_checkers=["ty"])
    for scope in policy.scopes:
        assert type(scope.policy) is ArchitecturePolicy
        assert not isinstance(scope.policy, Policy)
        for run_only in ("python", "type_checkers", "baseline", "scopes", "children", "ratchets", "mixed_purity", "effects", "coupling"):
            assert not hasattr(scope.policy, run_only), run_only
    assert policy.python == "3.12"


def test_architecture_fields_are_the_loaders_architecture_keys_plus_inherited_disabled_tools() -> None:
    """The dataclass split mirrors the TOML loader's own boundary: a field added to one side without the other fails here."""
    names = {f.name for f in fields(ArchitecturePolicy)}
    # raw TOML key -> field it becomes; parse-only keys (`ring`, `groups`, `middle_forbid`, `git_hook`) fold into others or into the root
    renamed = {"owned_literal": "owned_literals"}
    folded = {"ring", "groups", "middle_forbid", "git_hook"}
    expected = {renamed.get(key, key) for key in config._LEGACY_ARCHITECTURE_KEYS - folded} | {"disabled_tools"}
    assert names == expected
    assert not names & (config._SCOPED_ROOT_GLOBALS - {"disabled_tools"})


def test_an_architecture_policy_validates_itself() -> None:
    with pytest.raises(PolicyError, match="distinct ring names"):
        ArchitecturePolicy(rings=(), classify=())
    with pytest.raises(PolicyError, match="unknown ring"):
        ArchitecturePolicy(rings=(RingRule("a", frozenset(), frozenset()),), classify=(("x", "b"),))
    with pytest.raises(PolicyError, match="cannot be combined"):
        ArchitecturePolicy(rings=(RingRule("a", frozenset(), frozenset()),), classify=(), require_arbor_annotations=True, disabled_tools=("arbor-annotations",))
