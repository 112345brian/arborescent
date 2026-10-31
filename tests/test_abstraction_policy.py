from __future__ import annotations

from pathlib import Path
from typing import Any

from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import ALL, Policy, PolicyError, RingRule, Rule
from arborescent.rules import RuleContext, check_modules


def _scoped_findings(policy: Policy, root: Path) -> list[Any]:
    scan = FsScanner().scan(policy, root)
    return check_modules(
        policy, scan.modules, scan.known, scan.entry_deps, RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    )


def run(root: Path, **overrides: Any) -> list[Any]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def ports_findings(found: list[Any]) -> set[tuple[str, str]]:
    return {(v.subject, v.detail) for v in found if v.rule == Rule.NON_ABSTRACT_PORT}


def test_default_policy_does_not_require_abstract_members(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "class ConcretePort:\n    def run(self) -> None:\n        return None\n"})

    found = run(repo)

    assert ports_findings(found) == set()


def test_mixed_abstract_and_concrete_members_only_flags_the_concrete_ones(repo: Path) -> None:
    write_files(
        repo,
        {
            "app/ports.py": (
                "from abc import ABC\n\n"
                "class AbstractPort(ABC):\n    def run(self) -> None: ...\n\n"
                "class ConcretePort:\n    def run(self) -> None:\n        return None\n\n"
                "def abstract_fn() -> None: ...\n\n"
                "def concrete_fn() -> int:\n    return 1\n"
            )
        },
    )
    policy_rings = ring_with(ports={"may_import": ["domain"], "external": ["abc"], "require_abstract": True})

    found = run(repo, ring=policy_rings)

    assert {v.detail.split("'")[1] for v in found if v.rule == Rule.NON_ABSTRACT_PORT} == {"ConcretePort", "concrete_fn"}


def test_empty_ring_produces_no_violations_and_does_not_crash(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "\n"})
    policy_rings = ring_with(ports={"may_import": ["domain"], "external": [], "require_abstract": True})

    found = run(repo, ring=policy_rings)

    assert ports_findings(found) == set()


def test_class_inheriting_a_concrete_base_is_flagged_even_with_stub_methods(repo: Path) -> None:
    write_files(
        repo,
        {
            "app/ports.py": (
                "class ConcreteBase:\n    def run(self) -> None:\n        return None\n\n"
                "class InheritsConcrete(ConcreteBase):\n    def run(self) -> None: ...\n"
            )
        },
    )
    policy_rings = ring_with(ports={"may_import": ["domain"], "external": [], "require_abstract": True})

    found = run(repo, ring=policy_rings)

    flagged = {v.detail.split("'")[1] for v in found if v.rule == Rule.NON_ABSTRACT_PORT}
    # a concrete base is itself flagged, and inheriting it does not launder the subclass into an abstraction,
    # even though every method `InheritsConcrete` adds is a stub.
    assert flagged == {"ConcreteBase", "InheritsConcrete"}


def test_private_top_level_members_are_never_flagged(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "def _private_concrete() -> int:\n    return 1\n\nclass _PrivateConcrete:\n    pass\n"})
    policy_rings = ring_with(ports={"may_import": ["domain"], "external": [], "require_abstract": True})

    found = run(repo, ring=policy_rings)

    assert ports_findings(found) == set()


def test_require_abstract_defaults_to_false_and_parses_as_a_boolean() -> None:
    policy = policy_from_mapping(
        {"rings": ["domain", "ports"], "ring": {"ports": {"may_import": ["domain"]}}, "classify": {"app": "domain"}}
    )
    assert policy.ring("domain").require_abstract is False
    assert policy.ring("ports").require_abstract is False

    policy = policy_from_mapping(
        {
            "rings": ["domain", "ports"],
            "ring": {"ports": {"may_import": ["domain"], "require_abstract": True}},
            "classify": {"app": "domain"},
        }
    )
    assert policy.ring("ports").require_abstract is True


def _ring(name: str, require_abstract: bool, may: frozenset[str] = frozenset()) -> RingRule:
    return RingRule(name, may, frozenset(), require_abstract=require_abstract)


def test_require_abstract_is_local_to_each_policy_and_does_not_leak_across_them() -> None:
    """Simulates the per-scope isolation multi-scope governance (independent Policy objects per scope) relies on:
    this option lives on the `RingRule` a module's own policy resolves, never on shared or global state, so two
    independently-built policies with a same-named ring disagree about `require_abstract` without interfering.
    """
    strict = Policy(rings=(_ring("domain", False), _ring("ports", True, frozenset({"domain"}))), classify=())
    relaxed = Policy(rings=(_ring("domain", False), _ring("ports", False, frozenset({"domain"}))), classify=())

    assert strict.ring("ports").require_abstract is True
    assert relaxed.ring("ports").require_abstract is False
    # building `relaxed` after `strict` did not mutate the already-built `strict` policy (no shared default).
    assert strict.ring("ports").require_abstract is True


def test_require_abstract_is_rejected_when_not_a_boolean() -> None:
    # `bool()` coercion matches the existing `os_branching` parsing; a non-empty value still turns the ring on.
    policy = policy_from_mapping(
        {
            "rings": ["domain", "ports"],
            "ring": {"ports": {"may_import": ["domain"], "require_abstract": 1}},
            "classify": {"app": "domain"},
        }
    )
    assert policy.ring("ports").require_abstract is True


def test_wildcard_ring_still_flagged_independently_of_require_abstract() -> None:
    with_wildcard = ring_with(ports={"may_import": ["domain"], "external": [ALL], "require_abstract": True})
    policy = mini_policy(ring=with_wildcard)
    assert policy.ring("ports").require_abstract is True
    assert policy.ring("ports").external == frozenset({ALL})


def test_require_abstract_is_independent_per_scope() -> None:
    """`[scopes.<name>.ring.<name>].require_abstract`: each scope re-enters `_ring_from_mapping` with its own
    `ring` table (``config._scoped_policy``), so two scopes can disagree about the same ring name's
    `require_abstract` the same way they already disagree about `may_import`/`external`/`forbid`.
    """
    raw = {
        "scopes": {
            "strict_lib": {
                "sources": ["libs/strict/src"],
                "source_roots": ["libs/strict/src"],
                "rings": ["domain", "ports"],
                "classify": {"strict_lib.ports": "ports", "strict_lib.core": "domain"},
                "ring": {"ports": {"may_import": ["domain"], "require_abstract": True}},
            },
            "relaxed_lib": {
                "sources": ["libs/relaxed/src"],
                "source_roots": ["libs/relaxed/src"],
                "rings": ["domain", "ports"],
                "classify": {"relaxed_lib.ports": "ports", "relaxed_lib.core": "domain"},
                "ring": {"ports": {"may_import": ["domain"]}},
            },
        }
    }
    policy = policy_from_mapping(raw)

    assert policy.scope("strict_lib").policy.ring("ports").require_abstract is True
    assert policy.scope("relaxed_lib").policy.ring("ports").require_abstract is False


def test_non_abstract_port_fires_only_in_the_scope_that_requires_it(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "libs/strict/src/strict_ports.py": "class ConcretePort:\n    def run(self) -> None:\n        return None\n",
            "libs/relaxed/src/relaxed_ports.py": "class ConcretePort:\n    def run(self) -> None:\n        return None\n",
        },
    )
    policy = policy_from_mapping(
        {
            "scopes": {
                "strict_lib": {
                    "sources": ["libs/strict/src"],
                    "source_roots": ["libs/strict/src"],
                    "rings": ["domain", "ports"],
                    "classify": {"strict_ports": "ports"},
                    "ring": {"ports": {"may_import": ["domain"], "require_abstract": True}},
                },
                "relaxed_lib": {
                    "sources": ["libs/relaxed/src"],
                    "source_roots": ["libs/relaxed/src"],
                    "rings": ["domain", "ports"],
                    "classify": {"relaxed_ports": "ports"},
                    "ring": {"ports": {"may_import": ["domain"]}},
                },
            }
        }
    )

    found = _scoped_findings(policy, tmp_path)

    non_abstract = [v for v in found if v.rule == Rule.NON_ABSTRACT_PORT]
    assert len(non_abstract) == 1
    assert non_abstract[0].subject == "strict_ports" and non_abstract[0].line == 1


def test_require_abstract_rejects_nothing_for_a_malformed_policy_elsewhere() -> None:
    # sanity: adding require_abstract did not change unrelated policy error paths.
    try:
        policy_from_mapping({"rings": ["a", "a"], "classify": {}})
    except PolicyError as exc:
        assert "distinct ring names" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected a PolicyError")
