from __future__ import annotations

import pytest
from archlint_helpers import MINI, mini_policy, ring_with

from archlint.config import policy_from_mapping
from archlint.domain import ALL, Policy, PolicyError, RingRule


def test_allowed_rings_are_self_plus_declared_inner_rings() -> None:
    p = mini_policy()
    assert p.allowed_rings("domain") == {"domain"}
    assert p.allowed_rings("ports") == {"ports", "domain"}
    assert p.allowed_rings("composition") == {"composition", "adapters", "ports", "domain"}


def test_a_ring_cannot_import_an_outer_ring() -> None:
    with pytest.raises(PolicyError, match="onion"):
        _ = policy_from_mapping({**MINI, "ring": ring_with(domain={"may_import": ["adapters"]})})


def test_classify_must_name_a_known_ring() -> None:
    with pytest.raises(PolicyError, match="unknown ring"):
        _ = policy_from_mapping({**MINI, "classify": {"app": "nowhere"}})


def test_unknown_group_is_an_error() -> None:
    with pytest.raises(PolicyError, match="unknown group"):
        _ = policy_from_mapping({**MINI, "ring": ring_with(domain={"external": ["@missing"]})})


def test_ring_table_for_a_ring_that_is_not_listed_is_an_error() -> None:
    with pytest.raises(PolicyError, match="not in `rings`"):
        _ = policy_from_mapping({**MINI, "ring": ring_with(ghost={})})


def test_first_matching_pattern_wins_and_globs_cross_dots() -> None:
    p = mini_policy(classify={"app.special": "composition", "app.*": "domain"})
    assert p.ring_of("app.special") == "composition"
    assert p.ring_of("app.deep.module") == "domain"
    assert p.ring_of("other") is None


def test_layers_alone_express_a_policy_where_every_ring_may_import_all_inner_rings() -> None:
    assert mini_policy().expressible_by_layers()
    skipping = mini_policy(ring=ring_with(adapters={"may_import": ["domain"]}))  # adapters may not import ports
    assert not skipping.expressible_by_layers()


# --- the mapping translation (config layer) ---------------------------------------------------------------------


def test_groups_expand_and_defaults_apply() -> None:
    p = mini_policy()
    assert p.ring("domain").external == frozenset(["__future__", "dataclasses", "typing", "collections"])
    assert p.ring("composition").may_import == {ALL}
    assert p.ring("adapters").os_branching and not p.ring("domain").os_branching
    assert (p.python, p.type_checkers, p.baseline, p.output_dir) == ("3.11", ("pyright",), "archlint-baseline.json", ".")


@pytest.mark.parametrize("rings", [[], ["a", "a"]])
def test_rings_must_be_distinct_and_non_empty(rings: list[str]) -> None:
    with pytest.raises(PolicyError, match="distinct ring names"):
        _ = policy_from_mapping({"rings": rings})


@pytest.mark.parametrize("bad", ["huge", 3, None])
def test_unknown_preset_is_an_error(bad: object) -> None:
    with pytest.raises(PolicyError, match="is unknown; choose one of"):
        _ = mini_policy(preset=bad)


@pytest.mark.parametrize("bad", ["os", [""], [1], None])
def test_io_modules_must_be_a_list_of_names(bad: object) -> None:
    with pytest.raises(PolicyError, match="non-empty module names"):
        _ = mini_policy(io_modules=bad)


def test_errors_keep_their_order_when_several_apply() -> None:
    """Ring/onion problems are reported before a bad preset or io_modules, as before the split."""
    onion = ring_with(domain={"may_import": ["adapters"]})
    with pytest.raises(PolicyError, match="onion"):
        _ = mini_policy(ring=onion, preset="huge", io_modules="os")
    with pytest.raises(PolicyError, match="unknown ring"):
        _ = mini_policy(classify={"app": "nowhere"}, preset="huge")


# --- the value types hold their own invariants -----------------------------------------------------------------


def _rule(name: str, may: frozenset[str] = frozenset()) -> RingRule:
    return RingRule(name, may, frozenset())


def test_a_directly_built_policy_cannot_break_the_onion_rule() -> None:
    with pytest.raises(PolicyError, match="onion"):
        _ = Policy(rings=(_rule("a", frozenset({"b"})), _rule("b")), classify=())
    assert Policy(rings=(_rule("a"), _rule("b", frozenset({"a"}))), classify=()).allowed_rings("b") == {"a", "b"}
    assert Policy(rings=(_rule("a"), _rule("b", frozenset({ALL}))), classify=()).allowed_rings("b") == {"a", "b"}


def test_a_directly_built_policy_needs_distinct_rings_and_known_classify_targets() -> None:
    with pytest.raises(PolicyError, match="distinct ring names"):
        _ = Policy(rings=(), classify=())
    with pytest.raises(PolicyError, match="distinct ring names"):
        _ = Policy(rings=(_rule("a"), _rule("a")), classify=())
    with pytest.raises(PolicyError, match="unknown ring"):
        _ = Policy(rings=(_rule("a"),), classify=(("x", "nowhere"),))


def test_domain_does_not_know_the_config_shape() -> None:
    assert not hasattr(Policy, "from_mapping")
