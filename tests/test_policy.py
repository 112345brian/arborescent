from __future__ import annotations

import pytest
from arborescent_helpers import MINI, mini_policy, ring_with

from arborescent.adapters import LocalFileSystem
from arborescent.config import find_child_policy, load_policy, policy_from_mapping
from arborescent.domain import ALL, Policy, PolicyError, RingRule


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


def test_module_forbid_imports_are_parsed_and_accumulate_for_matching_globs() -> None:
    policy = mini_policy(module_forbid_imports={"app.adapters*": ["app.launcher"]})
    assert policy.forbids_module_import("app.adapters.bootstrap", "app.launcher.cli") == "app.launcher"


def test_custom_rule_configuration_is_named_for_the_check_label() -> None:
    policy = policy_from_mapping({
        **MINI,
        "ring": ring_with(domain={"external": ["@pure"], "forbid": ["clock"]}),
        "preset": "relaxed",
        "exclude": ["generated/**"],
        "suggestion_threshold": 450,
        "ruff": {"extend-select": ["UP"], "max-complexity": 12},
        "module_forbid_imports": {"app.adapters": ["app.launcher"]},
    })
    assert policy.customization_labels == ("ruff", "forbid", "thresholds", "preset", "exclude", "module import restrictions")


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
    assert (p.python, p.type_checkers, p.baseline, p.output_dir) == ("3.11", ("ty",), "arborescent-baseline.json", ".")
    assert p.forbid_circular_imports is True


def test_circular_import_policy_can_be_disabled_but_is_not_defaulted_off() -> None:
    assert mini_policy(forbid_circular_imports=False).forbid_circular_imports is False


def test_circular_import_policy_requires_a_boolean() -> None:
    with pytest.raises(PolicyError, match=r"forbid_circular_imports.*boolean"):
        _ = mini_policy(forbid_circular_imports="false")


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


def test_direct_policy_construction_uses_the_ty_default() -> None:
    assert Policy(rings=(_rule("a"),), classify=()).type_checkers == ("ty",)


def test_disabled_tools_are_parsed_as_an_intentional_opt_out() -> None:
    policy = policy_from_mapping({**MINI, "disabled_tools": ["ruff", "tach"]})

    assert policy.disabled_tools == ("ruff", "tach")


def test_arbor_annotations_is_accepted_as_a_disabled_tool() -> None:
    policy = policy_from_mapping({**MINI, "disabled_tools": ["arbor-annotations"]})

    assert policy.disabled_tools == ("arbor-annotations",)


@pytest.mark.parametrize("disabled", ["ruff", [""], ["pytest"], [1]])
def test_disabled_tools_must_be_known_tool_names(disabled: object) -> None:
    with pytest.raises(PolicyError, match="disabled_tools"):
        _ = policy_from_mapping({**MINI, "disabled_tools": disabled})


def test_resolve_ring_uses_valid_annotations_and_falls_back_for_invalid_ones() -> None:
    policy = mini_policy(classify={"app.*": "domain"})
    index = {
        "app.annotated": ("composition", None),
        "app.malformed": (None, "__arbor__ must be a dict with a ring name"),
        "app.unknown": ("missing", None),
    }

    assert policy.resolve_ring(index, "app.annotated") == "composition"
    assert policy.resolve_ring(index, "app.malformed") == "domain"
    assert policy.resolve_ring(index, "app.unknown") == "domain"
    assert policy.resolve_ring({}, "app.annotated") == "domain"


def test_annotation_violation_reports_malformed_and_unknown_annotations() -> None:
    policy = mini_policy()
    malformed_reason = "__arbor__ must be a dict with a ring name"
    index = {
        "app.valid": ("domain", None),
        "app.malformed": (None, malformed_reason),
        "app.unknown": ("missing", None),
    }

    assert policy.annotation_violation(index, "app.valid") is None
    assert policy.annotation_violation(index, "app.malformed") == malformed_reason
    assert policy.annotation_violation(index, "app.unknown") is not None
    assert policy.annotation_violation({}, "app.absent") is None


def test_disabling_arbor_annotations_ignores_annotation_resolution_and_violations() -> None:
    policy = mini_policy(classify={"app.*": "domain"}, disabled_tools=["arbor-annotations"])
    index = {"app.annotated": ("composition", None), "app.bad": (None, "malformed")}

    assert policy.resolve_ring(index, "app.annotated") == "domain"
    assert policy.annotation_violation(index, "app.bad") is None


# --- characterization: resolution with an empty classify table or no annotations at all ------------------------


def test_empty_classify_table_resolves_only_through_valid_annotations() -> None:
    policy = mini_policy(classify={})
    index = {"app.annotated": ("domain", None)}

    assert policy.ring_of("app.anything") is None
    assert policy.resolve_ring(index, "app.annotated") == "domain"
    assert policy.resolve_ring(index, "app.bare") is None
    assert policy.resolve_ring({}, "app.bare") is None


def test_no_annotations_at_all_falls_back_to_classify_for_every_module() -> None:
    policy = mini_policy(classify={"app.*": "domain"})

    assert policy.resolve_ring({}, "app.one") == "domain"
    assert policy.resolve_ring({}, "app.two") == "domain"
    assert policy.annotation_violation({}, "app.one") is None


def test_mixed_annotated_and_unannotated_modules_resolve_independently() -> None:
    policy = mini_policy(classify={"app.*": "domain"})
    index = {
        "app.annotated": ("composition", None),
        "app.invalid": (None, "__arbor__ must be a dict with a ring name"),
    }

    assert policy.resolve_ring(index, "app.annotated") == "composition"
    assert policy.resolve_ring(index, "app.invalid") == "domain"  # invalid annotation still falls back
    assert policy.resolve_ring(index, "app.unannotated") == "domain"  # absent from arbor_index entirely
    assert policy.annotation_violation(index, "app.annotated") is None
    assert policy.annotation_violation(index, "app.invalid") == "__arbor__ must be a dict with a ring name"
    assert policy.annotation_violation(index, "app.unannotated") is None


def test_multi_scope_resolution_is_independent_per_scope_with_mixed_annotations() -> None:
    policy = policy_from_mapping(_scoped_raw())
    index = {
        "alpha.core": ("adapters", None),  # valid annotation overrides this scope's own classify
        "shared.parser": (None, "__arbor__ must be a dict with a ring name"),  # invalid: falls back
    }

    assert policy.resolve_scope_ring("alpha", index, "alpha.core") == "adapters"
    assert policy.resolve_scope_ring("shared", index, "shared.parser") == "domain"
    assert policy.resolve_scope_ring("alpha", {}, "alpha.core") == "domain"  # no annotation index at all
    assert policy.scope_annotation_violation("shared", index, "shared.parser") is not None
    assert policy.scope_annotation_violation("alpha", index, "alpha.core") is None


def test_domain_does_not_know_the_config_shape() -> None:
    assert not hasattr(Policy, "from_mapping")


def _scoped_raw(**scopes: object) -> dict[str, object]:
    return {
        "python": "3.12",
        "disabled_tools": ["ruff"],
        "scopes": scopes or {
            "alpha": {
                "sources": ["libs/alpha/src"],
                "source_roots": ["libs/alpha/src"],
                "rings": ["domain", "adapters"],
                "classify": {"alpha.core": "domain"},
                "public": ["alpha.api*"],
            },
            "shared": {
                "sources": ["libs/shared/src"],
                "source_roots": ["libs/shared/src"],
                "rings": ["domain"],
                "classify": {"shared.parser": "domain"},
            },
        },
    }


def test_scope_policy_parses_local_rings_and_public_boundary() -> None:
    policy = policy_from_mapping(_scoped_raw())

    alpha = policy.scope("alpha")
    shared = policy.scope("shared")
    assert alpha.policy.ring_of("alpha.core") == "domain"
    assert alpha.public == ("alpha.api*",)
    assert alpha.depends_on == ()
    assert shared.policy.rings[0].name == "domain"
    assert policy.python == alpha.policy.python == "3.12"
    assert policy.disabled_tools == alpha.policy.disabled_tools == ("ruff",)
    assert policy.scope_for_path("libs/alpha/src/internal/service.py") == alpha
    assert policy.scope_for_path("libs/other/src/file.py") is None


def test_scopes_can_reuse_ring_names() -> None:
    policy = policy_from_mapping(_scoped_raw())

    assert policy.scope("alpha").policy.rings[0].name == "domain"
    assert policy.scope("shared").policy.rings[0].name == "domain"


def test_declared_children_can_coexist_with_parent_scopes() -> None:
    policy = policy_from_mapping({**_scoped_raw(), "children": ["packages/core"]})

    assert tuple(child.path for child in policy.children) == ("packages/core",)
    assert {scope.name for scope in policy.scopes} == {"alpha", "shared"}
    assert all(not scope.policy.children for scope in policy.scopes)


def test_scope_config_rejects_legacy_architecture_keys() -> None:
    with pytest.raises(PolicyError, match="cannot be combined with `scopes`"):
        _ = policy_from_mapping({**_scoped_raw(), "rings": ["domain"]})


def test_scope_config_rejects_overlapping_sources() -> None:
    raw = _scoped_raw()
    scopes = raw["scopes"]
    assert isinstance(scopes, dict)
    scopes["shared"] = {**scopes["shared"], "sources": ["libs/alpha/src/shared"]}

    with pytest.raises(PolicyError, match="source paths overlap"):
        _ = policy_from_mapping(raw)


def test_scope_config_rejects_unknown_or_cyclic_dependencies() -> None:
    unknown = _scoped_raw()
    scopes = unknown["scopes"]
    assert isinstance(scopes, dict)
    scopes["alpha"] = {**scopes["alpha"], "depends_on": ["missing"]}
    with pytest.raises(PolicyError, match="unknown scope"):
        _ = policy_from_mapping(unknown)

    cyclic = _scoped_raw()
    scopes = cyclic["scopes"]
    assert isinstance(scopes, dict)
    scopes["alpha"] = {**scopes["alpha"], "depends_on": ["shared"]}
    scopes["shared"] = {**scopes["shared"], "depends_on": ["alpha"]}
    with pytest.raises(PolicyError, match="dependency cycle"):
        _ = policy_from_mapping(cyclic)


def test_legacy_policy_parses_unchanged() -> None:
    policy = policy_from_mapping(MINI)

    assert policy.scopes == ()
    assert policy.rings == tuple(policy_from_mapping(MINI).rings)
    assert policy.sources == ("app",)


def test_child_arbors_are_parsed_as_independent_package_roots() -> None:
    policy = policy_from_mapping({**MINI, "children": ["packages/collection_core"]})

    assert tuple(child.path for child in policy.children) == ("packages/collection_core",)


@pytest.mark.parametrize(
    "children",
    [
        ["."],
        ["/absolute/package"],
            ["C:/external/package"],
        ["../outside"],
        ["packages/core", "packages/core"],
        ["packages", "packages/core"],
    ],
)
def test_child_arbor_paths_must_be_relative_unique_and_non_overlapping(children: list[str]) -> None:
    with pytest.raises(PolicyError, match="child"):
        _ = policy_from_mapping({**MINI, "children": children})


def test_child_arbor_requires_a_direct_policy_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "child").mkdir()
    (tmp_path / "arborescent.toml").write_text('rings = ["domain"]\n', encoding="utf-8")

    assert find_child_policy(LocalFileSystem(), tmp_path, "child") is None

    (tmp_path / "child" / "arborescent.toml").write_text('rings = ["domain"]\n', encoding="utf-8")
    assert find_child_policy(LocalFileSystem(), tmp_path, "child") == tmp_path / "child" / "arborescent.toml"

    (tmp_path / "child" / "arborescent.toml").unlink()
    (tmp_path / "child" / "pyproject.toml").write_text('[tool.arborescent]\nrings = ["domain"]\n', encoding="utf-8")
    assert find_child_policy(LocalFileSystem(), tmp_path, "child") == tmp_path / "child" / "pyproject.toml"
    (tmp_path / "child" / "pyproject.toml").write_text('[project]\nname = "no-policy"\n', encoding="utf-8")
    assert find_child_policy(LocalFileSystem(), tmp_path, "child") is None


def test_child_policy_mode_is_configurable() -> None:
    assert policy_from_mapping(MINI).child_policy_mode == "certify"
    assert policy_from_mapping({**MINI, "child_policy_mode": "parent"}).child_policy_mode == "parent"
    with pytest.raises(PolicyError, match="child_policy_mode"):
        policy_from_mapping({**MINI, "child_policy_mode": "ignore"})


def test_child_arbor_cannot_resolve_through_a_symlink_outside_parent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    parent = tmp_path / "parent"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    (parent / "child").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PolicyError, match="resolves outside"):
        find_child_policy(LocalFileSystem(), parent, "child")


# --- require_arbor_annotations: the ratchet toward annotations as source of truth -------------------------------


def test_require_arbor_annotations_is_named_for_the_check_label() -> None:
    assert mini_policy(require_arbor_annotations=True).customization_labels == ("annotations required",)


def test_require_arbor_annotations_defaults_to_false() -> None:
    assert mini_policy().require_arbor_annotations is False
    assert mini_policy().require_arbor_annotations_exclude == ()


def test_require_arbor_annotations_must_be_a_boolean() -> None:
    with pytest.raises(PolicyError, match=r"require_arbor_annotations.*boolean"):
        _ = mini_policy(require_arbor_annotations="true")


def test_require_arbor_annotations_conflicts_with_disabling_the_annotation_tool() -> None:
    with pytest.raises(PolicyError, match="require_arbor_annotations"):
        _ = mini_policy(require_arbor_annotations=True, disabled_tools=["arbor-annotations"])


def test_require_arbor_annotations_exclude_must_be_a_list_of_strings() -> None:
    with pytest.raises(PolicyError, match="require_arbor_annotations_exclude"):
        _ = mini_policy(require_arbor_annotations_exclude="app.*")


def test_annotation_required_violation_is_none_when_the_flag_is_off() -> None:
    policy = mini_policy(classify={"app.*": "domain"})
    assert policy.annotation_required_violation({}, "app.bare") is None


def test_annotation_required_violation_fires_only_for_classify_only_modules() -> None:
    policy = mini_policy(classify={"app.*": "domain"}, require_arbor_annotations=True)
    index = {
        "app.annotated": ("domain", None),
        "app.invalid": (None, "__arbor__ must be a dict with a ring name"),
    }

    assert policy.annotation_required_violation(index, "app.annotated") is None
    assert policy.annotation_required_violation(index, "app.invalid") is not None
    assert policy.annotation_required_violation({}, "app.unannotated") is not None
    assert policy.annotation_required_violation({}, "other.unclassified") is None  # unclassified's job, not this one's


def test_annotation_required_violation_respects_the_exclude_list() -> None:
    policy = mini_policy(
        classify={"app.*": "domain"}, require_arbor_annotations=True,
        require_arbor_annotations_exclude=["app.vendored*"],
    )

    assert policy.annotation_required_violation({}, "app.vendored.readonly") is None
    assert policy.annotation_required_violation({}, "app.owned") is not None


def test_require_arbor_annotations_is_independent_per_scope() -> None:
    raw = _scoped_raw()
    scopes = raw["scopes"]
    assert isinstance(scopes, dict)
    scopes["alpha"] = {**scopes["alpha"], "require_arbor_annotations": True}
    policy = policy_from_mapping(raw)

    assert policy.scope("alpha").policy.require_arbor_annotations is True
    assert policy.scope("shared").policy.require_arbor_annotations is False
    assert policy.scope_annotation_required_violation("alpha", {}, "alpha.core") is not None
    assert policy.scope_annotation_required_violation("shared", {}, "shared.parser") is None


def test_unclassified_detail_hints_when_a_glob_names_only_the_parent_package() -> None:
    p = policy_from_mapping({"rings": ["domain"], "classify": {"pkg": "domain"}})
    assert p.ring_of("pkg.sub.leaf") is None
    detail = p.unclassified_detail("pkg.sub.leaf")
    assert "did you mean 'pkg*'" in detail
    assert "did you mean" not in p.unclassified_detail("other.mod")
    assert "did you mean" not in p.unclassified_detail("pkg")


def test_scope_path_normalization_is_memoized_across_lookups() -> None:
    from arborescent import domain

    domain._NORMALIZED_SOURCE_PATHS.clear()
    assert domain._normalized_source_path("./libs/alpha/src/") == "libs/alpha/src"
    assert domain._NORMALIZED_SOURCE_PATHS == {"./libs/alpha/src/": "libs/alpha/src"}
    assert domain._normalized_source_path("./libs/alpha/src/") == "libs/alpha/src"


def _classify_from_repo(tmp_path, arbor: str, tach: str | None = None):  # type: ignore[no-untyped-def]
    (tmp_path / "arborescent.toml").write_text(arbor, encoding="utf-8")
    if tach is not None:
        (tmp_path / "tach.toml").write_text(tach, encoding="utf-8")
    return load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")[0]


TACH_LAYERS = '[[modules]]\npath = "pkg.core"\nlayer = "domain"\n[[modules]]\npath = "pkg.cli"\nlayer = "composition"\n[[modules]]\npath = "pkg.nolayer"\n'


def test_classify_from_derives_entries_from_tach_layers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    policy = _classify_from_repo(
        tmp_path, 'classify_from = "tach.toml"\nrings = ["domain", "composition"]\n', TACH_LAYERS,
    )
    assert policy.ring_of("pkg.core") == "domain"
    assert policy.ring_of("pkg.core.deep.leaf") == "domain"
    assert policy.ring_of("pkg.cli") == "composition"
    assert policy.ring_of("pkg.nolayer") is None


def test_classify_from_keeps_explicit_entries_first(tmp_path) -> None:  # type: ignore[no-untyped-def]
    policy = _classify_from_repo(
        tmp_path,
        'classify_from = "tach.toml"\nrings = ["domain", "composition"]\n[classify]\n"pkg.core.special" = "composition"\n"pkg.cli" = "domain"\n',
        TACH_LAYERS,
    )
    assert policy.ring_of("pkg.core.special") == "composition"
    assert policy.ring_of("pkg.cli") == "domain"  # explicit wins over the tach layer
    assert policy.ring_of("pkg.cli.sub") == "composition"


def test_classify_from_works_per_scope(tmp_path) -> None:  # type: ignore[no-untyped-def]
    policy = _classify_from_repo(
        tmp_path,
        '[scopes.alpha]\nsources = ["pkg"]\nsource_roots = ["."]\nrings = ["domain", "composition"]\nclassify_from = "tach.toml"\n',
        TACH_LAYERS,
    )
    assert policy.scope("alpha").policy.ring_of("pkg.core.x") == "domain"


def test_classify_from_reports_missing_or_malformed_files_and_unknown_layers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PolicyError, match="classify_from"):
        _classify_from_repo(tmp_path, 'classify_from = "nope.toml"\nrings = ["domain"]\n')
    with pytest.raises(PolicyError, match="classify_from"):
        _classify_from_repo(tmp_path, 'classify_from = "tach.toml"\nrings = ["domain"]\n', "not = [valid")
    with pytest.raises(PolicyError, match="classify_from"):
        _classify_from_repo(tmp_path, 'classify_from = 3\nrings = ["domain"]\n')
    with pytest.raises(PolicyError, match="unknown ring"):
        _classify_from_repo(tmp_path, 'classify_from = "tach.toml"\nrings = ["domain"]\n', TACH_LAYERS)
