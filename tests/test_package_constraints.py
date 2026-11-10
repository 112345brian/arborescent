"""``[package.constraints]`` (stage 4 of the host-owned-rings design, #164): schema and validation only, nothing enforced."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI

from arborescent.adapters import LocalFileSystem
from arborescent.config import load_policy, policy_from_mapping
from arborescent.domain import EFFECT_CATEGORIES, Policy, PolicyError
from arborescent.package_constraints import PackageConstraints
from arborescent.package_model import CatalogError, PackageDescriptor
from arborescent.package_parse import PACKAGE_KEYS, parse_catalog

CATALOG_ENTRY = 'name = "n"\nkind = "library"\nrepository = "r"\nroot = "."\nsummary = "s"\n'


def policy_with(package: object) -> Policy:
    return policy_from_mapping({**MINI, "package": package})


def constraints(**table: Any) -> PackageConstraints:
    return policy_with({"constraints": table}).package_constraints


# ---- pinned current behaviour (written before the change) ----------------------------------------------------


def test_the_catalog_allow_list_is_still_exactly_the_descriptor_fields() -> None:
    assert set(PACKAGE_KEYS) == {field.name for field in fields(PackageDescriptor)}


@pytest.mark.parametrize("key", ["constraints", "package", "must_be_pure", "forbid_effects"])
def test_constraints_are_not_packages_toml_metadata(key: str) -> None:
    """Separate from ``packages.toml``: a catalog entry carrying them is still an unknown key."""
    assert key not in PACKAGE_KEYS
    with pytest.raises(CatalogError, match=f"unknown key.*'{key}'"):
        parse_catalog({"packages": [{"id": "p", **_entry(), key: True}]})


def _entry() -> dict[str, str]:
    return {"name": "n", "kind": "library", "repository": "r", "root": ".", "summary": "s"}


def test_a_policy_without_a_package_table_is_unchanged() -> None:
    assert policy_from_mapping(MINI).package_constraints == PackageConstraints()
    assert PackageConstraints().must_be_pure is False
    assert PackageConstraints().forbid_effects == ()


def test_an_unrelated_package_key_in_the_policy_is_not_swallowed() -> None:
    """``package_catalog`` and friends are root keys, unaffected by the new ``package`` table."""
    assert policy_from_mapping({**MINI, "package_catalog": "packages.toml"}).package_catalog == "packages.toml"


# ---- the schema ----------------------------------------------------------------------------------------------


def test_a_full_constraints_table_parses() -> None:
    parsed = constraints(must_be_pure=True, forbid_effects=["network", "clock"])
    assert parsed == PackageConstraints(must_be_pure=True, forbid_effects=("network", "clock"))


def test_an_empty_constraints_table_is_no_constraints() -> None:
    assert constraints() == PackageConstraints()
    assert policy_with({}).package_constraints == PackageConstraints()


def test_every_scanner_effect_category_is_a_valid_name() -> None:
    assert set(constraints(forbid_effects=sorted(EFFECT_CATEGORIES)).forbid_effects) == EFFECT_CATEGORIES


def test_duplicate_effect_names_collapse_in_order() -> None:
    assert constraints(forbid_effects=["network", "clock", "network"]).forbid_effects == ("network", "clock")


def test_the_constraints_load_from_arborescent_toml_and_from_pyproject(tmp_path: Path) -> None:
    body = 'rings = ["domain"]\nsources = ["."]\nsource_roots = ["."]\n'
    table = "[package.constraints]\nmust_be_pure = true\nforbid_effects = [\"network\"]\n"
    expected = PackageConstraints(True, ("network",))
    (tmp_path / "arborescent.toml").write_text(body + table)
    assert load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")[0].package_constraints == expected
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "pyproject.toml").write_text(
        "[tool.arborescent]\n" + body + "\n[tool.arborescent.package.constraints]\n"
        'must_be_pure = true\nforbid_effects = ["network"]\n'
    )
    assert load_policy(LocalFileSystem(), sub / "pyproject.toml")[0].package_constraints == expected


# ---- no blind spot: everything unrecognised is an error ------------------------------------------------------


def test_a_misspelt_effect_name_is_rejected_and_named() -> None:
    with pytest.raises(PolicyError, match="netwrok"):
        _ = constraints(forbid_effects=["netwrok"])


@pytest.mark.parametrize("value", ["true", 1, 0, None, ["true"]])
def test_a_non_bool_must_be_pure_is_rejected(value: object) -> None:
    with pytest.raises(PolicyError, match="must_be_pure.*boolean"):
        _ = constraints(must_be_pure=value)


@pytest.mark.parametrize("value", ["network", 3, True, {"network": 1}, [1], [""], ["network", None]])
def test_forbid_effects_must_be_a_list_of_effect_names(value: object) -> None:
    with pytest.raises(PolicyError, match="forbid_effects"):
        _ = constraints(forbid_effects=value)


def test_an_unknown_constraint_key_is_an_error() -> None:
    with pytest.raises(PolicyError, match="unknown key.*must_be_pur"):
        _ = constraints(must_be_pur=True)


def test_an_unknown_key_under_package_is_an_error() -> None:
    with pytest.raises(PolicyError, match="unknown key.*constraint"):
        _ = policy_with({"constraint": {}})


@pytest.mark.parametrize("package", [[], "x", 1, True])
def test_package_must_be_a_table(package: object) -> None:
    with pytest.raises(PolicyError, match="`package` must be a table"):
        _ = policy_with(package)


@pytest.mark.parametrize("table", [[], "x", 1, True])
def test_constraints_must_be_a_table(table: object) -> None:
    with pytest.raises(PolicyError, match="`package.constraints` must be a table"):
        _ = policy_with({"constraints": table})


def test_constraints_are_not_an_arbor_annotation_or_a_scope_key() -> None:
    """Separate from ``__arbor__``: nothing in the scope tables accepts them, and a scoped policy keeps the root table."""
    scoped = {
        "scopes": {"a": {"sources": ["a"], "source_roots": ["."], "rings": ["domain"]}},
        "package": {"constraints": {"must_be_pure": True}},
    }
    assert policy_from_mapping(scoped).package_constraints == PackageConstraints(True, ())
    with pytest.raises(PolicyError, match="unknown key"):
        _ = policy_from_mapping({**scoped, "package": {"constraints": {"must_be_pure": True}, "x": 1}})
