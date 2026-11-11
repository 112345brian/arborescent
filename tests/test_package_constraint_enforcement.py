"""``[package.constraints]`` enforcement (stage 5 of the host-owned-rings design, #165).

Two rules: ``package-constraint-facts`` (a module uses a forbidden effect or import, whatever its ring) and
``package-constraint-placement`` (the module's ring permits what the constraint forbids). The first block pins what the
code did before the rules existed; the rest is the behaviour of the rules.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent import rules
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.config import policy_from_mapping
from arborescent.domain import Mutation, Policy, PolicyError, Rule, Violation
from arborescent.engine import Services, enforced
from arborescent.rules import RuleContext, check_modules
from arborescent.selftest import plan, selftest
from arborescent.testing import FakeToolRunner

FACTS = Rule.PACKAGE_CONSTRAINT_FACTS
PLACEMENT = Rule.PACKAGE_CONSTRAINT_PLACEMENT
CLOCK = "import time\n\nNOW = time.time()\n"


def check(root: Path, policy: Policy | None = None, **overrides: Any) -> list[Violation]:
    policy = policy or mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, context)


def pure_tree(root: Path, **files: str) -> None:
    """Every mini module reduced to a constant (the stock ones import ``os`` and ``sys``), then ``files`` on top."""
    write_files(root, {f"app/{name}.py": "X = 1\n" for name in ("domain", "ports", "adapters", "main")})
    write_files(root, {f"app/{name}.py": text for name, text in files.items()})


def constrained(root: Path, rule: Rule, **table: Any) -> list[Violation]:
    return [v for v in check(root, package={"constraints": table}) if v.rule == rule]


def subjects(found: list[Violation]) -> set[str]:
    return {v.subject for v in found}


# ---- pinned current behaviour (written before the change) ----------------------------------------------------


def test_a_package_with_no_constraints_is_unchanged(repo: Path) -> None:
    write_files(repo, {"app/main.py": CLOCK})
    before = check(repo)
    assert check(repo, package={}) == before
    assert check(repo, package={"constraints": {}}) == before
    assert not [v for v in before if v.rule.startswith("package-constraint")]


def test_constraints_that_are_declared_but_empty_enforce_nothing(repo: Path) -> None:
    write_files(repo, {"app/main.py": CLOCK})
    found = check(repo, package={"constraints": {"must_be_pure": False, "forbid_effects": []}})
    assert found == check(repo)


def test_the_existing_ring_rules_still_fire_alongside_constraints(repo: Path) -> None:
    write_files(repo, {"app/domain.py": CLOCK})
    plain = {(v.rule, v.subject) for v in check(repo)}
    assert ("effect-clock", "app.domain") in plain
    with_constraint = {(v.rule, v.subject) for v in check(repo, package={"constraints": {"forbid_effects": ["clock"]}})}
    assert plain <= with_constraint


@pytest.mark.parametrize("rule", [FACTS, PLACEMENT])
def test_package_gaps_demote_under_modularity_report_but_constraints_never_do(rule: Rule) -> None:
    """The ids start with ``package-`` like the catalog gaps, but a promise is not a modularity gap."""
    policy = mini_policy(package_catalog="packages.toml")
    reported = mini_policy(package_catalog="packages.toml", modularity="report")
    gap = Violation(Rule.PACKAGE_UNASSIGNED_MODULE, "app.x", "d")
    promise = Violation(rule, "app.x", "d")
    assert enforced(policy, [gap, promise]) == ([gap, promise], [])
    assert enforced(reported, [gap, promise]) == ([promise], [gap])


def test_an_unknown_effect_name_is_still_a_policy_error() -> None:
    with pytest.raises(PolicyError, match="netwrok"):
        _ = mini_policy(package={"constraints": {"forbid_effects": ["netwrok"]}})


# ---- package-constraint-facts ---------------------------------------------------------------------------------


def test_a_pure_claimed_package_fails_when_a_module_calls_the_clock(repo: Path) -> None:
    pure_tree(repo, main=CLOCK)
    found = constrained(repo, FACTS, must_be_pure=True)
    assert subjects(found) == {"app.main"}
    assert found[0].line == 3 and "clock" in found[0].detail and "time.time" in found[0].detail


def test_facts_fire_whatever_ring_the_module_is_in(repo: Path) -> None:
    """A host cannot loosen the promise by placement: the outer ring allows the clock, the constraint still bites."""
    write_files(repo, {"app/main.py": CLOCK})
    assert subjects(constrained(repo, FACTS, forbid_effects=["clock"])) == {"app.main"}
    loose = mini_policy(ring=ring_with(composition={"may_import": ["*"], "external": ["time"], "forbid": []}), package={"constraints": {"forbid_effects": ["clock"]}})
    assert subjects([v for v in check(repo, loose) if v.rule == FACTS]) == {"app.main"}


def test_facts_only_cover_the_named_effects(repo: Path) -> None:
    write_files(repo, {"app/main.py": CLOCK})
    assert constrained(repo, FACTS, forbid_effects=["network", "filesystem"]) == []


def test_facts_report_each_effect_use_once_per_site(repo: Path) -> None:
    write_files(repo, {"app/main.py": "import time\n\nA = time.time()\nB = 2\nC = time.time()\n"})
    assert sorted(v.line for v in constrained(repo, FACTS, forbid_effects=["clock"])) == [3, 5]


def test_pure_claimed_package_forbids_an_io_import_but_not_structural_ones(repo: Path) -> None:
    pure_tree(repo, main="from __future__ import annotations\n\nimport dataclasses\nimport math\nimport re\nimport socket\n\nX = 1\n")
    found = constrained(repo, FACTS, must_be_pure=True)
    assert [(v.line, "socket" in v.detail) for v in found] == [(6, True)]


def test_pure_claimed_package_forbids_a_third_party_import(repo: Path) -> None:
    pure_tree(repo, main="import requests\n\nX = 1\n")
    found = constrained(repo, FACTS, must_be_pure=True)
    assert len(found) == 1 and "requests" in found[0].detail


def test_a_type_only_import_does_not_run_so_it_does_not_count(repo: Path) -> None:
    pure_tree(repo, main="from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import requests\n")
    assert constrained(repo, FACTS, must_be_pure=True) == []


def test_a_function_level_import_still_counts(repo: Path) -> None:
    pure_tree(repo, main="def f() -> None:\n    import requests\n")
    assert len(constrained(repo, FACTS, must_be_pure=True)) == 1


def test_forbid_effects_alone_does_not_restrict_imports(repo: Path) -> None:
    write_files(repo, {"app/main.py": "import requests\n\nX = 1\n"})
    assert constrained(repo, FACTS, forbid_effects=["clock"]) == []


def test_must_be_pure_follows_first_party_imports_to_a_forbidden_effect(repo: Path) -> None:
    """File-level reachability, the existing effect-via-import graph: ``app.ports`` imports ``app.domain``, which reads the clock."""
    pure_tree(repo, domain=CLOCK, ports="from . import domain\n\nX = domain.NOW\n")
    found = constrained(repo, FACTS, must_be_pure=True)
    assert subjects(found) >= {"app.domain", "app.ports"}
    via = next(v for v in found if v.subject == "app.ports")
    assert via.line == 1 and "via app.domain;" in via.detail and "clock" in via.detail


def test_forbid_effects_alone_does_not_follow_imports(repo: Path) -> None:
    write_files(repo, {"app/domain.py": CLOCK, "app/ports.py": "from . import domain\n\nX = domain.NOW\n"})
    assert subjects(constrained(repo, FACTS, forbid_effects=["clock"])) == {"app.domain"}


def test_a_type_only_first_party_import_carries_no_reachability(repo: Path) -> None:
    pure_tree(repo, domain=CLOCK, ports="from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import domain\n")
    assert subjects(constrained(repo, FACTS, must_be_pure=True)) == {"app.domain"}


def test_a_clean_package_passes_both_rules_where_every_ring_forbids(repo: Path) -> None:
    only_inner = ring_with(composition={"may_import": ["*"], "external": ["sys"], "forbid": ["clock"]})
    policy = mini_policy(ring=only_inner, package={"constraints": {"forbid_effects": ["clock"]}})
    assert not [v for v in check(repo, policy) if v.rule in {FACTS, PLACEMENT}]


# ---- exemptions: tests, docs, developer tooling --------------------------------------------------------------


@pytest.mark.parametrize("path", ["tests/test_main.py", "tests/unit/test_main.py", "docs/conf.py", "test/test_main.py", "tests\\test_main.py"])
def test_test_and_doc_paths_are_exempt(repo: Path, path: str) -> None:
    write_files(repo, {"app/main.py": CLOCK})
    policy = mini_policy(package={"constraints": {"must_be_pure": True}})
    scan = FsScanner().scan(policy, repo)
    modules = {**scan.modules, "app.main": replace(scan.modules["app.main"], path=path)}
    found = check_modules(policy, modules, scan.known, scan.entry_deps, RuleContext(policy, scan.arbor_index, scan.scope_by_module))
    assert not [v for v in found if v.rule in {FACTS, PLACEMENT} and v.subject == "app.main"]


def test_a_source_path_that_only_mentions_tests_is_not_exempt(repo: Path) -> None:
    write_files(repo, {"app/main.py": CLOCK})
    policy = mini_policy(package={"constraints": {"must_be_pure": True}})
    scan = FsScanner().scan(policy, repo)
    modules = {**scan.modules, "app.main": replace(scan.modules["app.main"], path="app/tests_support/main.py")}
    found = check_modules(policy, modules, scan.known, scan.entry_deps, RuleContext(policy, scan.arbor_index, scan.scope_by_module))
    assert any(v.rule == FACTS and v.subject == "app.main" for v in found)


def test_developer_tooling_is_exempt(repo: Path) -> None:
    write_files(repo, {"devtools/__init__.py": "", "devtools/scan.py": CLOCK})
    found = check(repo, dev_tools=["devtools"], package={"constraints": {"must_be_pure": True}})
    assert not [v for v in found if v.rule in {FACTS, PLACEMENT} and v.subject.startswith("devtools")]


# ---- package-constraint-placement -----------------------------------------------------------------------------


def test_loosening_a_ring_fails_placement(repo: Path) -> None:
    """``app.ports`` is in the ports ring. Strict presets forbid every effect there; ``forbid = []`` opens the clock."""
    tight = constrained(repo, PLACEMENT, forbid_effects=["clock"])
    assert subjects(tight) == {"app.main"}  # the outermost ring permits everything and holds app.main
    loose = mini_policy(ring=ring_with(ports={"may_import": ["domain"], "external": ["@pure"], "forbid": []}), package={"constraints": {"forbid_effects": ["clock"]}})
    found = [v for v in check(repo, loose) if v.rule == PLACEMENT]
    assert subjects(found) == {"app.main", "app.ports"}
    ports = next(v for v in found if v.subject == "app.ports")
    assert "[ports]" in ports.detail and "clock" in ports.detail and "forbid_effects" in ports.detail


def test_placement_fires_before_any_module_misbehaves(repo: Path) -> None:
    """The point of the rule: the guard rail is wider than the promise even though no module uses the effect."""
    assert not [v for v in check(repo, package={"constraints": {"forbid_effects": ["network"]}}) if v.rule == FACTS]
    assert subjects(constrained(repo, PLACEMENT, forbid_effects=["network"])) == {"app.main"}


def test_placement_only_names_the_categories_the_ring_permits(repo: Path) -> None:
    loose = mini_policy(ring=ring_with(ports={"may_import": ["domain"], "external": ["@pure"], "forbid": ["network"]}), package={"constraints": {"forbid_effects": ["clock", "network"]}})
    ports = next(v for v in check(repo, loose) if v.rule == PLACEMENT and v.subject == "app.ports")
    assert "clock" in ports.detail and "network" not in ports.detail


def test_must_be_pure_places_every_module_in_a_ring_that_forbids_every_effect(repo: Path) -> None:
    found = constrained(repo, PLACEMENT, must_be_pure=True)
    assert subjects(found) == {"app.main"}
    assert "must_be_pure" in found[0].detail


def test_placement_ignores_the_patch_and_os_branching_categories(repo: Path) -> None:
    """``patch`` is a test-only seam and ``os-branching`` is not an effect category: neither makes a ring too loose to be pure."""
    only_patch_open = ring_with(composition={"may_import": ["*"], "external": ["sys"], "forbid": ["clock", "env", "identity", "randomness", "sleep", "threads", "process", "database", "network", "filesystem", "dynamic-import", "console", "global"]})
    policy = mini_policy(ring=only_patch_open, package={"constraints": {"must_be_pure": True}})
    assert not [v for v in check(repo, policy) if v.rule == PLACEMENT]


def test_an_unplaced_module_is_left_to_the_unclassified_rule(repo: Path) -> None:
    write_files(repo, {"app/stray.py": "X = 1\n"})
    found = check(repo, package={"constraints": {"must_be_pure": True}})
    assert any(v.rule == Rule.UNCLASSIFIED and v.subject == "app.stray" for v in found)
    assert not any(v.rule == PLACEMENT and v.subject == "app.stray" for v in found)


def test_an_annotation_that_loosens_a_module_fails_placement(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"ring": "composition"}\n\nVALUE: int = 1\n'})
    assert "app.domain" in subjects(constrained(repo, PLACEMENT, forbid_effects=["clock"]))


# ---- multi-scope ----------------------------------------------------------------------------------------------


def scoped_policy(**table: Any) -> Policy:
    def scope(name: str, classify: dict[str, str]) -> dict[str, Any]:
        return {"sources": [f"libs/{name}"], "source_roots": [f"libs/{name}"], "rings": ["domain", "adapters"], "classify": classify}

    return policy_from_mapping({
        "scopes": {
            "alpha": scope("alpha", {"alpha_core": "domain"}),
            "beta": scope("beta", {"beta_edge": "adapters"}),
        },
        "package": {"constraints": table},
    })


def test_each_module_is_judged_against_its_own_scopes_ladder(tmp_path: Path) -> None:
    write_files(tmp_path, {"libs/alpha/alpha_core.py": CLOCK, "libs/beta/beta_edge.py": "X = 1\n"})
    found = check(tmp_path, scoped_policy(forbid_effects=["clock"]))
    assert subjects([v for v in found if v.rule == PLACEMENT]) == {"beta_edge"}  # beta's adapters ring is its outermost: it permits the clock
    assert subjects([v for v in found if v.rule == FACTS]) == {"alpha_core"}  # facts follow the code, not the ladder


def test_a_scoped_policy_without_constraints_is_unchanged(tmp_path: Path) -> None:
    write_files(tmp_path, {"libs/alpha/alpha_core.py": CLOCK, "libs/beta/beta_edge.py": CLOCK})
    assert not [v for v in check(tmp_path, scoped_policy()) if v.rule in {FACTS, PLACEMENT}]


def test_reachability_does_not_cross_scopes(tmp_path: Path) -> None:
    """The existing effect graph is per scope; cross-scope leaks are ``scope-effect-leak``'s business."""
    write_files(tmp_path, {"libs/alpha/alpha_core.py": CLOCK, "libs/beta/beta_edge.py": "import alpha_core\n"})
    found = [v for v in check(tmp_path, scoped_policy(must_be_pure=True)) if v.rule == FACTS]
    assert subjects(found) == {"alpha_core"}


# ---- selftest probes: each rule has a mutation, and the probe fails when the rule is removed --------------------


def services_for(root: Path) -> Services:
    return Services(FsScanner(root / "cache.json"), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), [])


def planned(root: Path, policy: Policy) -> tuple[list[Mutation], list[str]]:
    return plan(policy, FsScanner().scan(policy, root))


def test_no_constraints_means_no_constraint_probes(repo: Path) -> None:
    mutations, skipped = planned(repo, mini_policy())
    assert not [m for m in mutations if m.rule in {FACTS, PLACEMENT}]
    assert not [reason for reason in skipped if "package-constraint" in reason]


def test_both_rules_get_a_mutation_when_constraints_are_declared(repo: Path) -> None:
    mutations, _ = planned(repo, mini_policy(package={"constraints": {"forbid_effects": ["clock"]}}))
    by_rule = {m.rule: m for m in mutations if m.rule in {FACTS, PLACEMENT}}
    assert set(by_rule) == {FACTS, PLACEMENT}
    assert by_rule[FACTS].new_file and "time.time()" in by_rule[FACTS].content
    assert by_rule[PLACEMENT].new_file and '"ring": "composition"' in by_rule[PLACEMENT].content


def test_a_probe_is_skipped_with_a_reason_when_it_cannot_be_built(repo: Path) -> None:
    every_ring_forbids = ring_with(composition={"may_import": ["*"], "external": ["sys"], "forbid": ["clock"]})
    mutations, skipped = planned(repo, mini_policy(ring=every_ring_forbids, package={"constraints": {"forbid_effects": ["clock"]}}))
    assert PLACEMENT not in {m.rule for m in mutations} and FACTS in {m.rule for m in mutations}
    assert any(reason.startswith("package-constraint-placement:") for reason in skipped)
    # an effect no probe spells is reported, never silently dropped
    _, skipped = planned(repo, mini_policy(package={"constraints": {"forbid_effects": ["database"]}}))
    assert any(reason.startswith("package-constraint-facts:") for reason in skipped)


def test_the_placement_probe_is_built_for_each_scope(tmp_path: Path) -> None:
    write_files(tmp_path, {"libs/alpha/alpha_core.py": "X = 1\n", "libs/beta/beta_edge.py": "X = 1\n"})
    mutations, _ = planned(tmp_path, scoped_policy(forbid_effects=["clock"]))
    assert sorted(m.file for m in mutations if m.rule == PLACEMENT) == ["libs/alpha/arborescent_probe_constraint.py", "libs/beta/arborescent_probe_constraint.py"]


def test_selftest_kills_both_constraint_probes(repo: Path) -> None:
    report = selftest(mini_policy(package={"constraints": {"forbid_effects": ["clock"]}}), repo, services_for(repo))
    assert report.survived == []
    assert {FACTS, PLACEMENT} <= {rule for rule in (*Rule,) if any(entry.startswith(f"[{rule}]") for entry in report.killed)}


@pytest.mark.parametrize(("hook", "rule"), [("use_violations", FACTS), ("placement_violations", PLACEMENT)])
def test_removing_a_rule_makes_its_probe_survive(repo: Path, monkeypatch: pytest.MonkeyPatch, hook: str, rule: Rule) -> None:
    monkeypatch.setattr(rules, hook, lambda *args, **kwargs: [])
    report = selftest(mini_policy(package={"constraints": {"forbid_effects": ["clock"]}}), repo, services_for(repo))
    assert [entry for entry in report.survived if entry.startswith(f"[{rule}]")], report.survived


def test_removing_reachability_leaves_the_direct_checks_working(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pure_tree(repo, domain=CLOCK, ports="from . import domain\n\nX = domain.NOW\n")
    monkeypatch.setattr(rules, "reach_violations", lambda *args, **kwargs: [])
    found = constrained(repo, FACTS, must_be_pure=True)
    assert subjects(found) == {"app.domain"}  # only the reach finding for app.ports went away
