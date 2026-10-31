"""Strict linting and strict modularity vary independently: the ``modularity`` key and ``--require-modularity``."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services
from test_package_gaps import repo

from arborescent.domain import PolicyError, Rule
from arborescent.engine import Options, _child_arbor_command, run_check, run_own  # pyright: ignore[reportPrivateUsage]

OPTS = Options(tools=False, use_baseline=False)
REQUIRE = Options(tools=False, use_baseline=False, require_modularity=True)


def policy(**overrides: object):
    return mini_policy(package_catalog="packages.toml", **overrides)


@pytest.mark.parametrize("value", ["strict", "", 1, None])
def test_modularity_rejects_values_other_than_ready_and_report(value: object) -> None:
    with pytest.raises(PolicyError, match="modularity"):
        policy(modularity=value)


def test_modularity_report_needs_a_catalog() -> None:
    with pytest.raises(PolicyError, match="package_catalog"):
        mini_policy(modularity="report")


def test_modularity_defaults_to_ready() -> None:
    assert policy().modularity == "ready"
    assert mini_policy().modularity == "ready"  # no catalog: nothing to enforce, and not an error


@pytest.mark.parametrize("preset", ["strict", "standard", "relaxed"])
def test_every_lint_preset_accepts_a_catalog_and_either_mode(preset: str) -> None:
    assert policy(preset=preset, modularity="report").modularity == "report"
    assert policy(preset=preset, modularity="ready").modularity == "ready"


def test_ready_fails_the_check_on_a_gap(tmp_path: Path) -> None:
    repo(tmp_path, {"app/static/site.css": "p {}\n"})
    report = run_check(policy(), tmp_path, services(), OPTS)
    assert Rule.PACKAGE_UNOWNED_ASSET in {v.rule for v in report.violations}
    assert not report.ok


def test_report_demotes_gaps_to_notes_and_passes(tmp_path: Path) -> None:
    repo(tmp_path, {"app/static/site.css": "p {}\n"})
    report = run_check(policy(modularity="report"), tmp_path, services(), OPTS)
    assert not [v for v in report.violations if str(v.rule).startswith("package-")]
    assert Rule.PACKAGE_UNOWNED_ASSET in {v.rule for v in report.notes}
    assert report.ok


def test_report_still_returns_the_gaps_from_run_own_so_the_selftest_can_probe_them(tmp_path: Path) -> None:
    repo(tmp_path, {"app/static/site.css": "p {}\n"})
    found, _ = run_own(policy(modularity="report"), tmp_path, services())
    assert Rule.PACKAGE_UNOWNED_ASSET in {v.rule for v in found}


def test_a_relaxed_lint_repo_can_be_modularity_ready(tmp_path: Path) -> None:
    repo(tmp_path)
    report = run_check(policy(preset="relaxed"), tmp_path, services(), REQUIRE)
    assert report.modularity_failures == ()
    assert report.modularity_certified
    assert report.ok


def test_require_modularity_passes_with_a_cut_list(tmp_path: Path) -> None:
    repo(tmp_path)  # the fixture's package-spans-rings note is information, not a gap
    report = run_check(policy(), tmp_path, services(), REQUIRE)
    assert {n.rule for n in report.notes} == {Rule.PACKAGE_SPANS_RINGS}
    assert report.modularity_failures == ()


def test_require_modularity_fails_without_a_catalog(tmp_path: Path) -> None:
    repo(tmp_path)
    report = run_check(mini_policy(), tmp_path, services(), REQUIRE)
    assert any("package_catalog" in f for f in report.modularity_failures)
    assert not report.ok


def test_require_modularity_fails_in_report_mode(tmp_path: Path) -> None:
    repo(tmp_path)
    report = run_check(policy(modularity="report"), tmp_path, services(), REQUIRE)
    assert any("report" in f for f in report.modularity_failures)
    assert not report.ok


def test_require_modularity_fails_on_an_open_gap(tmp_path: Path) -> None:
    repo(tmp_path, {"app/static/site.css": "p {}\n"})
    report = run_check(policy(), tmp_path, services(), REQUIRE)
    assert any("site.css" in f for f in report.modularity_failures)
    assert not report.modularity_certified


def test_require_modularity_is_passed_to_child_arbors(tmp_path: Path) -> None:
    assert "--require-modularity" in _child_arbor_command(tmp_path / "arborescent.toml", REQUIRE)
    assert "--require-modularity" not in _child_arbor_command(tmp_path / "arborescent.toml", OPTS)


def test_a_declared_package_cycle_fails_require_modularity(tmp_path: Path) -> None:
    """Found by mutation: a catalog edit making two packages depend on each other used to be certified."""
    repo(tmp_path)
    text = (tmp_path / "packages.toml").read_text(encoding="utf-8")
    (tmp_path / "packages.toml").write_text(text.replace("[[packages]]\nid = \"core\"", "[[packages]]\nid = \"core\"\ndepends_on = [\"shell\"]", 1), encoding="utf-8")
    report = run_check(policy(), tmp_path, services(), REQUIRE)
    assert any("package-dependency-cycle" in f for f in report.modularity_failures)
    assert not report.ok


def test_a_repository_with_one_all_owning_package_is_not_certified(tmp_path: Path) -> None:
    """#73: no partition means nothing for the ownership and dependency checks to enforce, so there is nothing to certify."""
    repo(tmp_path)
    one = (
        '[[packages]]\nid = "all"\nname = "All"\nkind = "application"\nrepository = "example/repo"\nroot = "app"\n'
        'summary = "everything"\nunmanaged = true\n[[packages.paths]]\npath = "app"\nrole = "source"\n'
        '[[packages.paths]]\npath = "app/templates"\nrole = "assets"\n[[assets]]\npattern = "app/**/*.sql"\npackage = "all"\n'
    )
    (tmp_path / "packages.toml").write_text(one, encoding="utf-8")
    plain = run_check(policy(), tmp_path, services(), OPTS)
    assert Rule.PACKAGE_OWNS_EVERYTHING in {n.rule for n in plain.notes}
    assert plain.ok  # plain `check` only notes it
    report = run_check(policy(), tmp_path, services(), REQUIRE)
    assert any("package-owns-everything" in f for f in report.modularity_failures)
    assert not report.ok
