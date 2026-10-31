"""#56: per-package release, pin and history policy annotations, their validation, and the missing-policy gap rule."""

from __future__ import annotations

import tomllib
from dataclasses import fields
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

import arborescent.engine as engine
from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.domain import Rule
from arborescent.engine import Options, run_check, run_own
from arborescent.package_gaps import load_package_context
from arborescent.package_model import CatalogError, PackageDescriptor, ReleasePolicy
from arborescent.package_parse import PACKAGE_KEYS, parse_catalog
from arborescent.selftest import plan, selftest

FS = LocalFileSystem()

HEAD = 'id = "core"\nname = "Core"\nkind = "library"\nrepository = "example/repo"\nroot = "app"\nsummary = "s"\n'


def entry(extra: str = "") -> dict[str, object]:
    return tomllib.loads(f"[[packages]]\n{HEAD}{extra}")


def parsed(extra: str = ""):
    return parse_catalog(entry(extra)).packages[0]


# ---- parsing -------------------------------------------------------------------------------------------------


def test_a_package_without_policy_fields_parses_with_no_policy() -> None:
    package = parsed()
    assert (package.release, package.history, package.unmanaged) == (None, None, False)


def test_the_worked_example_parses_into_a_typed_policy() -> None:
    package = parsed(
        'history = "preserve"\n[packages.release]\npin = "tag"\nmerge = "merge-commit"\ntag_before_branch_delete = true\n'
    )
    assert package.release == ReleasePolicy(pin="tag", merge="merge-commit", tag_before_branch_delete=True)
    assert package.history == "preserve"
    assert not package.unmanaged


def test_each_release_field_is_optional() -> None:
    assert parsed('[packages.release]\npin = "sha"\n').release == ReleasePolicy(pin="sha")
    assert parsed("[packages.release]\n").release == ReleasePolicy()


def test_unmanaged_is_an_explicit_marker() -> None:
    assert parsed("unmanaged = true\n").unmanaged


@pytest.mark.parametrize("value", ["fresh", "preserve"])
def test_history_accepts_its_two_values(value: str) -> None:
    assert parsed(f'history = "{value}"\n').history == value


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('history = "rewrite"\n', "history"),
        ("history = true\n", "history"),
        ('[packages.release]\npin = "branch"\n', "release.pin"),
        ("[packages.release]\npin = 1\n", "release.pin"),
        ('[packages.release]\nmerge = "squash"\n', "release.merge"),
        ('[packages.release]\nmerge = "rebase"\n', "release.merge"),
        ('[packages.release]\ntag_before_branch_delete = "yes"\n', "tag_before_branch_delete"),
        ("[packages.release]\ntag_before_branch_delete = 1\n", "tag_before_branch_delete"),
        ('[packages.release]\npin = "tag"\nversioning = "semver"\n', "versioning"),
        ('release = "tag"\n', "release must be a table"),
        ('unmanaged = "yes"\n', "unmanaged"),
        ("unmanaged = false\n", "unmanaged"),
    ],
)
def test_invalid_values_and_unknown_release_keys_are_rejected(extra: str, message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        parsed(extra)


@pytest.mark.parametrize(
    "extra",
    ['history = "preserve"\n', '[packages.release]\npin = "tag"\n'],
)
def test_unmanaged_cannot_be_combined_with_a_policy(extra: str) -> None:
    with pytest.raises(CatalogError, match="unmanaged"):
        parsed("unmanaged = true\n" + extra)


def test_a_rejected_key_names_the_package() -> None:
    with pytest.raises(CatalogError, match="package 'core'"):
        parsed('[packages.release]\nbogus = 1\n')


# ---- the gap rule and its probe --------------------------------------------------------------------------


CATALOG = f"""
[[packages]]
{HEAD}
history = "preserve"
[packages.release]
pin = "tag"
merge = "merge-commit"
tag_before_branch_delete = true
[[packages.paths]]
path = "app"
role = "source"
"""


def repo(root: Path, catalog: str = CATALOG) -> None:
    write_files(root, {**MINI_FILES, "packages.toml": catalog})


def policy_with_catalog():
    return mini_policy(package_catalog="packages.toml")


def gaps(root: Path) -> list[str]:
    found, _ = run_own(policy_with_catalog(), root, services())
    return sorted(v.subject for v in found if v.rule == Rule.PACKAGE_RELEASE_POLICY_MISSING)


def test_a_package_with_a_policy_is_not_a_gap(tmp_path: Path) -> None:
    repo(tmp_path)
    assert gaps(tmp_path) == []


def test_history_alone_counts_as_a_policy(tmp_path: Path) -> None:
    repo(tmp_path, f'[[packages]]\n{HEAD}history = "fresh"\n[[packages.paths]]\npath = "app"\nrole = "source"\n')
    assert gaps(tmp_path) == []


def test_an_empty_release_table_counts_as_a_policy(tmp_path: Path) -> None:
    repo(tmp_path, f'[[packages]]\n{HEAD}[packages.release]\n[[packages.paths]]\npath = "app"\nrole = "source"\n')
    assert gaps(tmp_path) == []


def test_a_package_with_neither_policy_nor_marker_is_a_named_gap(tmp_path: Path) -> None:
    bare = f'[[packages]]\n{HEAD}[[packages.paths]]\npath = "app"\nrole = "source"\n'
    repo(tmp_path, bare)
    assert gaps(tmp_path) == ["core"]


def test_the_unmanaged_marker_satisfies_the_gap(tmp_path: Path) -> None:
    repo(tmp_path, f'[[packages]]\n{HEAD}unmanaged = true\n[[packages.paths]]\npath = "app"\nrole = "source"\n')
    assert gaps(tmp_path) == []


def test_a_policy_gap_fails_the_check_and_is_baselinable(tmp_path: Path) -> None:
    repo(tmp_path, f'[[packages]]\n{HEAD}[[packages.paths]]\npath = "app"\nrole = "source"\n')
    report = run_check(policy_with_catalog(), tmp_path, services(), Options(tools=False, use_baseline=False))
    assert Rule.PACKAGE_RELEASE_POLICY_MISSING in {v.rule for v in report.violations}
    assert not report.ok
    assert Rule.PACKAGE_RELEASE_POLICY_MISSING not in {note.rule for note in report.notes}


def test_the_rule_is_off_without_a_catalog(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    found, _ = run_own(mini_policy(), tmp_path, services())
    assert Rule.PACKAGE_RELEASE_POLICY_MISSING not in {v.rule for v in found}


def mutated_rules(root: Path) -> set[Rule]:
    policy = policy_with_catalog()
    scan = FsScanner().scan(policy, root)
    context = load_package_context(policy, root, FS, scan)
    return {m.rule for m in plan(policy, scan, context)[0]}


def test_the_rule_has_a_selftest_mutation_when_a_catalog_is_configured(tmp_path: Path) -> None:
    repo(tmp_path)
    assert Rule.PACKAGE_RELEASE_POLICY_MISSING in mutated_rules(tmp_path)


def test_selftest_kills_the_policy_mutation(tmp_path: Path) -> None:
    repo(tmp_path)
    report = selftest(policy_with_catalog(), tmp_path, services())
    assert report.survived == []
    assert any(entry.startswith(f"[{Rule.PACKAGE_RELEASE_POLICY_MISSING}]") for entry in report.killed)


def test_selftest_survives_when_the_policy_check_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard against rule removal: with only the policy rule stubbed out, its probe must report a survivor."""
    repo(tmp_path)
    original = engine.package_gap_violations
    monkeypatch.setattr(
        engine,
        "package_gap_violations",
        lambda *a, **k: [v for v in original(*a, **k) if v.rule != Rule.PACKAGE_RELEASE_POLICY_MISSING],
    )
    report = selftest(policy_with_catalog(), tmp_path, services())
    assert any(entry.startswith(f"[{Rule.PACKAGE_RELEASE_POLICY_MISSING}]") for entry in report.survived)


# ---- the whole-entry key check and workspace members (with #54's `home`) ------------------------------------


def test_the_allowed_package_keys_are_exactly_the_descriptor_fields() -> None:
    """A new descriptor field must be accepted by the catalog, and no key may be accepted without a field."""
    assert set(PACKAGE_KEYS) == {field.name for field in fields(PackageDescriptor)}


@pytest.mark.parametrize("key", ["histroy", "releases", "owner"])
def test_an_unknown_package_key_is_rejected_and_named(key: str) -> None:
    with pytest.raises(CatalogError, match=f"unknown key.*'{key}'"):
        parsed(f'{key} = "x"\n')


def test_every_known_package_key_is_still_accepted() -> None:
    package = parsed(
        'domain = "d"\ncapabilities = ["c"]\ndepends_on = []\nextraction_status = "in-repository"\npublic = ["app.*"]\n'
        'externals = []\nunmanaged = true\n[packages.home]\nkind = "repository"\nrepository = "https://github.com/org/core.git"\n'
    )
    assert package.home is not None and package.unmanaged


MEMBER = '[packages.home]\nkind = "workspace-member"\npath = "packages/core"\n'


def test_a_workspace_member_cannot_declare_release_or_history() -> None:
    with pytest.raises(CatalogError, match="workspace member"):
        parsed('history = "preserve"\n' + MEMBER)
    with pytest.raises(CatalogError, match="workspace member"):
        parsed('[packages.release]\npin = "tag"\n' + MEMBER)


def test_a_workspace_member_may_be_marked_unmanaged_or_carry_no_policy() -> None:
    assert parsed("unmanaged = true\n" + MEMBER).unmanaged
    assert parsed(MEMBER).release is None


def test_a_repository_home_may_carry_a_policy() -> None:
    package = parsed('history = "fresh"\n[packages.home]\nkind = "repository"\nrepository = "https://github.com/org/core.git"\n')
    assert package.history == "fresh"


def test_the_gap_check_exempts_workspace_members_only(tmp_path: Path) -> None:
    member = f'[[packages]]\n{HEAD}{MEMBER}[[packages.paths]]\npath = "app"\nrole = "source"\n'
    repo(tmp_path, member)
    assert gaps(tmp_path) == []
    own = member.replace(MEMBER, '[packages.home]\nkind = "repository"\nrepository = "https://github.com/org/core.git"\n')
    repo(tmp_path, own)
    assert gaps(tmp_path) == ["core"]
