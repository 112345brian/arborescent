"""#52 end to end: the opt-in ``package_catalog`` policy key, the gap rules inside ``check``, and their selftest probes."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

import arborescent.engine as engine
from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.cli import main
from arborescent.domain import INFORMATIONAL_RULES, PolicyError, Rule
from arborescent.engine import Options, run_check, run_own
from arborescent.package_gaps import load_package_catalog, load_package_context
from arborescent.package_model import CatalogError
from arborescent.selftest import plan, selftest

FS = LocalFileSystem()

CATALOG = """
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/repo"
root = "app"
summary = "domain and ports"
history = "preserve"
[[packages.paths]]
path = "app/__init__.py"
role = "source"
[[packages.paths]]
path = "app/domain.py"
role = "source"
[[packages.paths]]
path = "app/ports.py"
role = "source"

[[packages]]
id = "shell"
name = "Shell"
kind = "application"
repository = "example/repo"
root = "app"
summary = "adapters and entry point"
unmanaged = true
depends_on = ["core"]
[[packages.paths]]
path = "app/adapters.py"
role = "source"
[[packages.paths]]
path = "app/main.py"
role = "source"
[[packages.paths]]
path = "app/templates"
role = "assets"

[[assets]]
pattern = "app/**/*.sql"
package = "core"
"""

PACKAGE_RULES = {
    Rule.PACKAGE_UNASSIGNED_MODULE, Rule.PACKAGE_MULTIPLY_ASSIGNED_MODULE, Rule.PACKAGE_UNOWNED_ASSET,
    Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET, Rule.PACKAGE_SPANS_RINGS, Rule.PACKAGE_UNDECLARED_DEPENDENCY,
    Rule.PACKAGE_RELEASE_POLICY_MISSING,
}


def repo(root: Path, extra: dict[str, str] | None = None) -> None:
    write_files(root, {**MINI_FILES, "app/queries.sql": "select 1;\n", "app/templates/page.html": "<p></p>\n", "packages.toml": CATALOG, **(extra or {})})


def policy_with_catalog(**overrides: object):
    return mini_policy(package_catalog="packages.toml", **overrides)


def package_findings(root: Path, **overrides: object) -> list[tuple[str, str]]:
    found, _ = run_own(policy_with_catalog(**overrides), root, services())
    return sorted((v.rule, v.subject) for v in found if v.rule in PACKAGE_RULES)


def test_the_check_is_off_unless_a_catalog_is_configured(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)  # no packages.toml at all
    found, _ = run_own(mini_policy(), tmp_path, services())
    assert not {v.rule for v in found} & PACKAGE_RULES


def test_the_catalog_key_needs_the_strict_preset() -> None:
    with pytest.raises(PolicyError, match="strict"):
        mini_policy(package_catalog="packages.toml", preset="relaxed")


@pytest.mark.parametrize("value", [5, "", "/abs/packages.toml", "../packages.toml"])
def test_the_catalog_key_must_be_a_relative_path(value: object) -> None:
    with pytest.raises(PolicyError, match="package_catalog"):
        mini_policy(package_catalog=value)


def test_a_missing_catalog_file_is_an_error_not_a_pass(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    with pytest.raises(CatalogError, match="cannot read package catalog"):
        run_own(policy_with_catalog(), tmp_path, services())


def test_a_fully_assigned_repository_reports_only_the_ring_span_information(tmp_path: Path) -> None:
    repo(tmp_path)
    assert package_findings(tmp_path) == [(Rule.PACKAGE_SPANS_RINGS, "core"), (Rule.PACKAGE_SPANS_RINGS, "shell")]


def test_gaps_are_reported_by_rule_id(tmp_path: Path) -> None:
    repo(tmp_path, {"app/extra.py": "X = 1\n", "app/static/site.css": "a {}\n"})
    assert (Rule.PACKAGE_UNASSIGNED_MODULE, "app.extra") in package_findings(tmp_path)
    assert (Rule.PACKAGE_UNOWNED_ASSET, "app/static/site.css") in package_findings(tmp_path)


def test_a_module_claimed_twice_is_reported_even_though_catalog_validation_would_refuse_the_catalog(tmp_path: Path) -> None:
    repo(tmp_path, {"packages.toml": CATALOG + '\n[[packages]]\nid = "dup"\nname = "Dup"\nkind = "library"\n'
                    'repository = "r"\nroot = "app"\nsummary = "s"\n[[packages.paths]]\npath = "app/domain.py"\nrole = "source"\n'})
    assert (Rule.PACKAGE_MULTIPLY_ASSIGNED_MODULE, "app.domain") in package_findings(tmp_path)


def test_an_undeclared_cross_package_import_is_reported(tmp_path: Path) -> None:
    repo(tmp_path, {"packages.toml": CATALOG.replace('depends_on = ["core"]\n', "")})
    assert (Rule.PACKAGE_UNDECLARED_DEPENDENCY, "app.adapters") in package_findings(tmp_path)


def test_only_a_strict_policy_with_scopes_is_unaffected_by_the_asset_universe_of_other_files(tmp_path: Path) -> None:
    repo(tmp_path, {"README.md": "# docs\n", "tests/data.json": "{}\n"})  # outside the policy's sources
    assert not any(rule == Rule.PACKAGE_UNOWNED_ASSET for rule, _ in package_findings(tmp_path))


def test_policy_excludes_and_generated_output_are_not_assets(tmp_path: Path) -> None:
    repo(tmp_path, {"app/skipme/a.txt": "x", "app/__pycache__/m.cpython-313.pyc": "x", "app/build/out.bin": "x"})
    rules = [v for v in package_findings(tmp_path, exclude=["skipme"]) if v[0] == Rule.PACKAGE_UNOWNED_ASSET]
    assert rules == []


def test_ring_span_information_never_blocks_a_check_or_enters_the_baseline(tmp_path: Path) -> None:
    repo(tmp_path)
    report = run_check(policy_with_catalog(), tmp_path, services(), Options(tools=False, use_baseline=False))
    assert {note.rule for note in report.notes} == {Rule.PACKAGE_SPANS_RINGS}
    assert not {v.rule for v in report.violations} & INFORMATIONAL_RULES
    assert report.ok


def test_a_gap_fails_the_check(tmp_path: Path) -> None:
    repo(tmp_path, {"app/extra.py": "X = 1\n"})
    report = run_check(policy_with_catalog(), tmp_path, services(), Options(tools=False, use_baseline=False))
    assert Rule.PACKAGE_UNASSIGNED_MODULE in {v.rule for v in report.violations}
    assert not report.ok


def test_the_baseline_command_never_records_information(tmp_path: Path) -> None:
    repo(tmp_path)
    write_files(tmp_path, {"arborescent.toml": 'package_catalog = "packages.toml"\nsources = ["app"]\nrings = ["domain"]\n'
                           '[ring.domain]\nexternal = ["*"]\n[classify]\n"app*" = "domain"\n'})
    assert main(["--config", str(tmp_path / "arborescent.toml"), "baseline"]) == 0
    assert "package-spans-rings" not in (tmp_path / "arborescent-baseline.json").read_text(encoding="utf-8")


def test_the_loader_returns_assets_and_packages(tmp_path: Path) -> None:
    repo(tmp_path)
    document = load_package_catalog(FS, tmp_path, policy_with_catalog())
    assert [p.id for p in document.packages] == ["core", "shell"]
    assert [a.package for a in document.assets] == ["core"]


def mutated_rules(root: Path) -> set[Rule]:
    policy = policy_with_catalog()
    scan = FsScanner().scan(policy, root)
    context = load_package_context(policy, root, FS, scan)
    return {m.rule for m in plan(policy, scan, context)[0]}


def test_every_package_rule_has_a_selftest_mutation_when_a_catalog_is_configured(tmp_path: Path) -> None:
    repo(tmp_path)
    assert mutated_rules(tmp_path) >= PACKAGE_RULES


def test_no_package_mutation_is_planned_without_a_catalog(tmp_path: Path) -> None:
    repo(tmp_path)
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)
    assert not {m.rule for m in plan(policy, scan)[0]} & PACKAGE_RULES


def test_selftest_kills_every_package_rule_mutation(tmp_path: Path) -> None:
    repo(tmp_path)
    report = selftest(policy_with_catalog(), tmp_path, services())
    assert report.survived == []
    for rule in PACKAGE_RULES:
        assert any(entry.startswith(f"[{rule}]") for entry in report.killed), rule


def test_selftest_survives_when_the_gap_check_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard against rule removal: with the check stubbed out, the probes must report survivors."""
    repo(tmp_path)
    monkeypatch.setattr(engine, "package_gap_violations", lambda *args, **kwargs: [])
    report = selftest(policy_with_catalog(), tmp_path, services())
    assert {entry.split("]")[0].lstrip("[") for entry in report.survived} >= PACKAGE_RULES
