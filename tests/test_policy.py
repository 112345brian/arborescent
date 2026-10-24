from __future__ import annotations

import pytest

from archlint.domain import Policy, PolicyError
from archlint_helpers import MINI, ring_with, mini_policy


def test_allowed_rings_are_self_plus_declared_inner_rings() -> None:
    p = mini_policy()
    assert p.allowed_rings("domain") == {"domain"}
    assert p.allowed_rings("ports") == {"ports", "domain"}
    assert p.allowed_rings("composition") == {"composition", "adapters", "ports", "domain"}


def test_a_ring_cannot_import_an_outer_ring() -> None:
    with pytest.raises(PolicyError, match="onion"):
        _ = Policy.from_mapping({**MINI, "ring": ring_with(domain={"may_import": ["adapters"]})})


def test_classify_must_name_a_known_ring() -> None:
    with pytest.raises(PolicyError, match="unknown ring"):
        _ = Policy.from_mapping({**MINI, "classify": {"app": "nowhere"}})


def test_unknown_group_is_an_error() -> None:
    with pytest.raises(PolicyError, match="unknown group"):
        _ = Policy.from_mapping({**MINI, "ring": ring_with(domain={"external": ["@missing"]})})


def test_ring_table_for_a_ring_that_is_not_listed_is_an_error() -> None:
    with pytest.raises(PolicyError, match="not in `rings`"):
        _ = Policy.from_mapping({**MINI, "ring": ring_with(ghost={})})


def test_first_matching_pattern_wins_and_globs_cross_dots() -> None:
    p = mini_policy(classify={"app.special": "composition", "app.*": "domain"})
    assert p.ring_of("app.special") == "composition"
    assert p.ring_of("app.deep.module") == "domain"
    assert p.ring_of("other") is None


def test_layers_alone_express_a_policy_where_every_ring_may_import_all_inner_rings() -> None:
    assert mini_policy().expressible_by_layers()
    skipping = mini_policy(ring=ring_with(adapters={"may_import": ["domain"]}))  # adapters may not import ports
    assert not skipping.expressible_by_layers()
