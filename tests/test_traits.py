"""Ring `traits` (#222, #223): the vendored catalog, what it compiles to, and the move off the keys it replaced.

``traits_before.json`` was captured from the last code that read `forbid`, `forbid_at_import`, `require_abstract`,
`require_typed_ports`, `os_branching` and `middle_forbid` (a618387): each old config and the ring rules it produced.
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import (
    MINI_FILES,
    RINGS,
    mini_policy,
    ring_with,
    services,
    write_files,
)

from arborescent.cli import main
from arborescent.config import load_policy, policy_from_mapping
from arborescent.domain import ArchitecturePolicy, Policy, PolicyError, Rule, Violation
from arborescent.engine import Options, child_arbor_runs
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.package_model import CatalogError
from arborescent.package_tree import shipped_policy
from arborescent.presets import (
    AT_IMPORT_TRAITS,
    CATEGORY_TRAITS,
    DEFAULT_FORBID_CATEGORIES,
    EFFECT_CATEGORIES,
    EFFECT_TRAITS,
    TRAITS,
)
from arborescent.tool_runner import SubprocessToolRunner

REPOSITORY = Path(__file__).resolve().parents[1]
BEFORE: dict[str, Any] = json.loads((Path(__file__).parent / "traits_before.json").read_text(encoding="utf-8"))
REMOVED = ("forbid", "forbid_at_import", "require_abstract", "require_typed_ports", "os_branching")


def one(*traits: str, ring: str = "ports", **settings: Any) -> ArchitecturePolicy:
    may = [] if ring == "domain" else ["domain"]
    return mini_policy(ring=ring_with(**{ring: {"may_import": may, "traits": list(traits)}}), **settings)


# ---- the catalog ------------------------------------------------------------------------------------------------


def test_every_effect_category_has_a_decided_trait_name() -> None:
    """A new category fails here until someone names its trait (and so decides what `no-effects` grows to cover)."""
    assert set(CATEGORY_TRAITS) == set(EFFECT_CATEGORIES)
    assert len(set(CATEGORY_TRAITS.values())) == len(CATEGORY_TRAITS)
    assert {c: "no-sync-primitives" if c == "state" else f"no-{c}" for c in CATEGORY_TRAITS} == CATEGORY_TRAITS


def test_the_catalog_is_exactly_the_decided_one() -> None:
    singles = set(CATEGORY_TRAITS.values())

    assert set(EFFECT_TRAITS) == singles | {"no-module-state", "no-effects", "permits-effects"}
    assert set(AT_IMPORT_TRAITS) == {f"{trait}-at-import" for trait in singles} | {"no-effects-at-import"}
    assert {*EFFECT_TRAITS, *AT_IMPORT_TRAITS, "require-abstract", "require-typed-ports", "permits-os-branching"} == TRAITS
    assert EFFECT_TRAITS["no-module-state"] == {"global", "registry", "cache"}
    assert EFFECT_TRAITS["no-effects"] == AT_IMPORT_TRAITS["no-effects-at-import"] == EFFECT_CATEGORIES


def test_the_documentation_names_every_trait() -> None:
    text = (REPOSITORY / "docs" / "configuration.md").read_text(encoding="utf-8")
    section = text.split("## Traits", 1)[1].split("\n## ", 1)[0]

    missing = [trait for trait in sorted(TRAITS) if not trait.endswith("-at-import") and f"`{trait}`" not in section]

    assert missing == [] and "`no-effects-at-import`" in section and "`no-<category>-at-import`" in section


# ---- what a trait list compiles to ------------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(EFFECT_CATEGORIES))
def test_a_single_trait_forbids_its_category_and_dynamic_import(category: str) -> None:
    policy = one(CATEGORY_TRAITS[category])

    assert policy.ring("ports").forbid == {category}
    assert policy.forbidden_effects("ports") == {category, "dynamic-import"}
    assert one(f"{CATEGORY_TRAITS[category]}-at-import").ring("ports").forbid_at_import == {category}


def test_effect_traits_add_up_and_replace_the_positional_default() -> None:
    policy = one("no-module-state", "no-clock", ring="domain")

    assert policy.forbidden_effects("domain") == {"global", "registry", "cache", "clock", "dynamic-import"}
    assert one("no-effects").forbidden_effects("ports") == EFFECT_CATEGORIES


@pytest.mark.parametrize("traits", [
    [], ["require-abstract"], ["require-typed-ports"], ["permits-os-branching"], ["no-env-at-import"], ["no-effects-at-import"],
])
@pytest.mark.parametrize("ring", ["domain", "ports", "composition"])
def test_without_a_call_time_effect_trait_the_positional_default_stands(traits: list[str], ring: str) -> None:
    table = {**RINGS[ring], "traits": traits}

    policy = mini_policy(ring=ring_with(**{ring: table}))

    assert policy.ring(ring).forbid is None
    assert policy.forbidden_effects(ring) == mini_policy().forbidden_effects(ring)
    assert policy.customization_labels == ()


def test_omitted_traits_and_an_empty_list_are_the_same() -> None:
    assert mini_policy(ring=ring_with(domain={"external": ["@pure"], "traits": []})) == mini_policy()
    assert mini_policy().forbidden_effects("domain") == DEFAULT_FORBID_CATEGORIES


def test_permits_effects_is_the_explicit_allow_all() -> None:
    policy = one("permits-effects", "no-env-at-import", "require-abstract", ring="domain")

    assert policy.ring("domain").forbid == frozenset()
    assert policy.forbidden_effects("domain") == frozenset()
    assert policy.ring("domain").forbid_at_import == {"env"}  # calling may, importing may not (#230)
    assert policy.customization_labels == ("effect traits",)


@pytest.mark.parametrize("other", ["no-clock", "no-module-state", "no-effects", "no-sync-primitives"])
def test_permits_effects_excludes_every_call_time_effect_trait(other: str) -> None:
    with pytest.raises(PolicyError, match=rf"`permits-effects` allows every effect.*{other}"):
        one("permits-effects", other)


@pytest.mark.parametrize(("flag", "field"), [
    ("require-abstract", "require_abstract"), ("require-typed-ports", "require_typed_ports"), ("permits-os-branching", "os_branching"),
])
def test_the_structural_traits_set_their_fields(flag: str, field: str) -> None:
    assert getattr(one(flag).ring("ports"), field) is True
    assert getattr(one().ring("ports"), field) is False


def test_a_repeated_trait_is_one_trait() -> None:
    assert one("no-clock", "no-clock") == one("no-clock")


@pytest.mark.parametrize("value", ["no-clock", True, 1, {}, [""], [1], [None], [["no-clock"]]])
def test_traits_must_be_a_list_of_names(value: object) -> None:
    with pytest.raises(PolicyError, match=r"`traits` in \[ring\.ports\] must be a list"):
        mini_policy(ring=ring_with(ports={"may_import": ["domain"], "traits": value}))


@pytest.mark.parametrize(("name", "meant"), [
    ("clock", "no-clock"), ("state", "no-sync-primitives"), ("no-clok", "no-clock"), ("no_clock", "no-clock"),
    ("No-Effects", "no-effects"), ("require_abstract", "require-abstract"),
])
def test_an_unknown_trait_names_the_one_meant(name: str, meant: str) -> None:
    with pytest.raises(PolicyError, match=rf"unknown trait in \[ring\.ports\]\.traits: '{name}' \(did you mean '{meant}'\?\)"):
        one(name)


@pytest.mark.parametrize("name", ["no-io", "pure", "no-state", "no-module-state-at-import", "no-clock\0", "x" * 5000])
def test_names_outside_the_catalog_are_refused(name: str) -> None:
    with pytest.raises(PolicyError, match="unknown trait"):
        one(name)


@pytest.mark.parametrize("name", ["@nondet", "@unknown"])
def test_a_group_cannot_define_traits(name: str) -> None:
    with pytest.raises(PolicyError, match="a group cannot define one"):
        one(name, groups={"nondet": ["no-clock"], "pure": ["typing"]})


def test_traits_are_per_scope() -> None:
    scope = {"rings": ["domain"]}
    policy = policy_from_mapping({"scopes": {
        "a": {**scope, "sources": ["a"], "source_roots": ["a"], "ring": {"domain": {"traits": ["no-env"]}}},
        "b": {**scope, "sources": ["b"], "source_roots": ["b"]},
    }})

    assert policy.scope("a").policy.forbidden_effects("domain") == {"env", "dynamic-import"}
    assert policy.scope("b").policy.forbidden_effects("domain") == frozenset()
    assert policy.customization_labels == ("effect traits",)


# ---- the removed keys -------------------------------------------------------------------------------------------

_LINE = re.compile(r"^\s*\[(?:scopes\.(?P<scope>\w+)\.)?ring\.(?P<ring>\w+)\] .*?: (?P<traits>traits = \[.*\])$")


def converted(config: dict[str, Any]) -> dict[str, Any]:
    """Apply, mechanically, exactly what the error for ``config`` says; the policy it names must then load."""
    try:
        policy_from_mapping(config)
    except PolicyError as exc:
        message = str(exc)
    else:
        return config  # nothing removed was used
    out = copy.deepcopy(config)
    for table in [out, *out.get("scopes", {}).values()]:
        table.pop("middle_forbid", None)
        for ring in table.get("ring", {}).values():
            for key in REMOVED:
                ring.pop(key, None)
    for line in message.splitlines()[1:]:
        if "delete `middle_forbid`" in line:
            continue
        match = _LINE.match(line)
        assert match, line
        table = out["scopes"][match["scope"]] if match["scope"] else out
        rings = table.setdefault("ring", {})
        rings.setdefault(match["ring"], {}).update(tomllib.loads(match["traits"]))
    return out


def dump(policy: Policy) -> dict[str, Any]:
    scopes = [(f"{scope.name}.", scope.policy) for scope in policy.scopes] if policy.scopes else [("", policy)]
    return {
        prefix + rule.name: {
            "forbid": None if rule.forbid is None else sorted(rule.forbid),
            "effective": sorted(local.forbidden_effects(rule.name)),
            "at_import": sorted(rule.forbid_at_import),
            "os": rule.os_branching, "abstract": rule.require_abstract, "typed": rule.require_typed_ports,
            "ratio": rule.max_reexport_ratio,
        }
        for prefix, local in scopes for rule in local.rings
    }


@pytest.mark.parametrize("shape", sorted(BEFORE))
def test_the_printed_replacement_means_what_the_old_keys_meant(shape: str) -> None:
    old, rules = BEFORE[shape]["config"], BEFORE[shape]["rules"]

    now = dump(policy_from_mapping(converted(old)))

    # `middle_forbid` was a preset override: its rings had no `forbid` of their own and now carry one. Nothing reads that
    # field but `forbidden_effects` (compared as `effective`) and the innermost-ring certification it never reached.
    if "middle_forbid" in json.dumps(old):
        for name, rule in rules.items():
            if rule["forbid"] is None and now[name]["forbid"] is not None:
                rule = {**rule, "forbid": now[name]["forbid"]}
                assert sorted({*now[name]["forbid"], *(["dynamic-import"] if now[name]["forbid"] else [])}) == rule["effective"]
            assert now[name] == rule
        assert now.keys() == rules.keys()
    else:
        assert now == rules


def test_this_repository_enforces_what_it_did_before_its_migration() -> None:
    policy, _ = load_policy(LocalFileSystem(), REPOSITORY / "pyproject.toml")

    assert dump(policy) == BEFORE["this_repository"]["rules"]


def test_one_error_lists_every_change_with_groups_written_out() -> None:
    with pytest.raises(PolicyError) as error:
        mini_policy(
            groups={"pure": ["typing"], "nondet": ["clock", "state"]},
            middle_forbid=["env"],
            ring=ring_with(domain={"forbid": ["@nondet", "env", "clock"], "require_abstract": True}),
        )

    message = str(error.value)
    assert "delete `middle_forbid` from the policy root" in message
    assert (
        '[ring.domain] remove `forbid`, `require_abstract` and set: '
        'traits = ["no-clock", "no-sync-primitives", "no-env", "require-abstract"]'
    ) in message
    assert '[ring.ports] add: traits = ["no-env"]' in message
    assert '[ring.adapters] add: traits = ["permits-os-branching", "no-env"]' in message  # keeps what it had
    assert "[ring.composition]" not in message  # outermost: `middle_forbid` never reached it


@pytest.mark.parametrize(("value", "left"), [
    ("env", "`forbid` (not a list)"), (["@nope"], "'@nope'"), (["clocks"], "'clocks'"), ([1, None], "1, None"), (["a\0b"], r"'a\x00b'"),
])
def test_a_list_with_no_trait_equivalent_says_so_and_never_allows_everything(value: object, left: str) -> None:
    with pytest.raises(PolicyError) as error:
        mini_policy(ring=ring_with(domain={"forbid": value}))

    assert f"by hand, no trait for: {left}" in str(error.value)
    assert "permits-effects" not in str(error.value)


def test_a_half_migrated_ring_keeps_its_traits_in_the_replacement() -> None:
    with pytest.raises(PolicyError, match=re.escape('traits = ["permits-os-branching", "no-env"]')):
        mini_policy(ring=ring_with(domain={"traits": ["permits-os-branching"], "forbid": ["env"]}))


@pytest.mark.parametrize("settings", [
    {"middle_forbid": 3}, {"middle_forbid": [], "rings": "domain"}, {"ring": {"domain": {"forbid": []}}, "groups": 5},
    {"scopes": {"a": {"ring": 5, "middle_forbid": None}}}, {"scopes": {"a": 5}, "middle_forbid": ["env"]},
])
def test_removed_keys_in_a_malformed_policy_are_still_policy_errors(settings: dict[str, Any]) -> None:
    with pytest.raises(PolicyError, match="replaced these keys"):
        policy_from_mapping(settings)


def test_a_scope_is_named_in_its_lines() -> None:
    with pytest.raises(PolicyError) as error:
        policy_from_mapping(BEFORE["scoped"]["config"])

    assert "delete `middle_forbid` from [scopes.x]" in str(error.value)
    assert "[scopes.y.ring.c] remove `forbid`, `os_branching` and set:" in str(error.value)


# Each caller of `load_policy`, and what it does with the refusal.

OLD = 'rings = ["domain"]\n\n[ring.domain]\nforbid = ["env"]\n\n[classify]\n"*" = "domain"\n'
NEW_LINE = 'traits = ["no-env"]'


def test_the_command_line_and_bench_report_it_and_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"arborescent.toml": OLD})

    for argv in (["--config", str(tmp_path / "arborescent.toml"), "check"], ["bench", str(tmp_path)]):
        assert main(argv) == 2
        assert NEW_LINE in capsys.readouterr().err


def test_a_shipped_policy_with_a_removed_key_is_invalid(tmp_path: Path) -> None:
    write_files(tmp_path, {"arborescent.toml": OLD})

    with pytest.raises(CatalogError, match=re.escape(NEW_LINE)):
        shipped_policy(LocalFileSystem(), tmp_path, tmp_path / "arborescent.toml")


def test_a_child_arbor_with_a_removed_key_fails_the_parent_run(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "child/arborescent.toml": OLD})

    (run,) = child_arbor_runs(mini_policy(children=["child"]), tmp_path, services(SubprocessToolRunner(tmp_path)), Options())

    assert run.status == "fail"
    assert NEW_LINE in "\n".join(run.lines)


# The baseline key of a placement finding was written when rings had `forbid`; it must not move.


def test_the_placement_finding_keeps_its_baseline_key_and_explains_the_new_spelling() -> None:
    detail = "[domain] ring permits clock, which the package forbids; tighten the ring's `forbid` or drop the constraint"
    found = Violation(Rule.PACKAGE_CONSTRAINT_PLACEMENT, "app.x", detail)

    assert found.key == f"package-constraint-placement|app.x|{detail}"
    assert "`traits`" in found.render()
    source = (REPOSITORY / "src/arborescent/package_constraint_rules.py").read_text(encoding="utf-8")
    assert "tighten the ring's `forbid` or drop the constraint" in source  # changing it would orphan baseline entries
