"""#52: every module and asset belongs to exactly one package. The pure rules, then the glob and catalog parsing."""

from __future__ import annotations

from typing import Any

import pytest

from arborescent.domain import INFORMATIONAL_RULES, Rule, Violation
from arborescent.package_assignment import RepositoryFacts, assignment_violations
from arborescent.package_fields import glob_match
from arborescent.package_model import (
    AssetRule,
    CatalogDocument,
    CatalogError,
    PackageDescriptor,
    PackagePath,
)
from arborescent.package_parse import parse_catalog


def package(identifier: str, *paths: str, depends_on: tuple[str, ...] = ()) -> PackageDescriptor:
    return PackageDescriptor(
        identifier, identifier, "library", "example/repo", "src", "summary",
        depends_on=depends_on, paths=tuple(PackagePath(path, "source") for path in paths),
    )


def facts(
    modules: dict[str, str] | None = None, *, rings: dict[str, str | None] | None = None,
    imports: dict[str, tuple[str, ...]] | None = None, assets: tuple[str, ...] = (),
) -> RepositoryFacts:
    modules = modules or {}
    return RepositoryFacts(modules, rings or dict.fromkeys(modules, "domain"), imports or {}, assets)


def found(catalog: CatalogDocument, data: RepositoryFacts, rule: Rule) -> list[Violation]:
    return [v for v in assignment_violations(catalog, data) if v.rule == rule]


def test_a_module_in_no_package_is_unassigned() -> None:
    catalog = CatalogDocument((package("core", "src/core"),))
    [violation] = found(catalog, facts({"core.a": "src/core/a.py", "stray.b": "src/stray/b.py"}), Rule.PACKAGE_UNASSIGNED_MODULE)
    assert violation.subject == "stray.b"
    assert "src/stray/b.py" in violation.detail


def test_a_module_claimed_by_two_packages_names_both() -> None:
    catalog = CatalogDocument((package("core", "src/core"), package("extra", "src/core/a.py")))
    [violation] = found(catalog, facts({"core.a": "src/core/a.py", "core.b": "src/core/b.py"}), Rule.PACKAGE_MULTIPLY_ASSIGNED_MODULE)
    assert violation.subject == "core.a"
    assert "core" in violation.detail and "extra" in violation.detail


def test_exact_file_and_directory_claims_do_not_match_by_prefix_of_a_name() -> None:
    catalog = CatalogDocument((package("core", "src/core"),))
    [violation] = found(catalog, facts({"corex.a": "src/corex/a.py"}), Rule.PACKAGE_UNASSIGNED_MODULE)
    assert violation.subject == "corex.a"


def test_an_asset_matching_no_path_and_no_glob_is_unowned() -> None:
    catalog = CatalogDocument((package("core", "src/core"),), (AssetRule("src/web/**", "core"),))
    [violation] = found(catalog, facts(assets=("src/core/t.html", "src/web/x.css", "src/other/y.sql")), Rule.PACKAGE_UNOWNED_ASSET)
    assert violation.subject == "src/other/y.sql"


def test_asset_globs_are_first_match_wins_so_they_never_double_claim() -> None:
    catalog = CatalogDocument(
        (package("a", "src/a"), package("b", "src/b")),
        (AssetRule("data/**", "a"), AssetRule("data/**/*.sql", "b")),
    )
    data = facts(assets=("data/x/q.sql",))
    assert assignment_violations(catalog, data) == []


def test_an_asset_inside_one_package_path_and_matched_by_another_packages_glob_is_doubly_assigned() -> None:
    catalog = CatalogDocument((package("a", "src/a"), package("b", "src/b")), (AssetRule("src/a/**/*.html", "b"),))
    [violation] = found(catalog, facts(assets=("src/a/t/page.html",)), Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET)
    assert violation.subject == "src/a/t/page.html"
    assert "a" in violation.detail and "b" in violation.detail


def test_a_glob_that_agrees_with_the_owning_path_is_not_a_second_claim() -> None:
    catalog = CatalogDocument((package("a", "src/a"),), (AssetRule("src/a/**/*.html", "a"),))
    assert assignment_violations(catalog, facts(assets=("src/a/t/page.html",))) == []


def test_a_package_spanning_rings_is_reported_as_information() -> None:
    catalog = CatalogDocument((package("core", "src/core"),))
    data = facts(
        {"core.a": "src/core/a.py", "core.b": "src/core/b.py"}, rings={"core.a": "domain", "core.b": "adapters"},
    )
    [violation] = found(catalog, data, Rule.PACKAGE_SPANS_RINGS)
    assert violation.subject == "core"
    assert "adapters" in violation.detail and "domain" in violation.detail
    assert Rule.PACKAGE_SPANS_RINGS in INFORMATIONAL_RULES


def test_a_package_inside_one_ring_is_not_reported() -> None:
    catalog = CatalogDocument((package("core", "src/core"),))
    assert found(catalog, facts({"core.a": "src/core/a.py", "core.b": "src/core/b.py"}), Rule.PACKAGE_SPANS_RINGS) == []


def test_unclassified_modules_do_not_count_as_a_ring() -> None:
    catalog = CatalogDocument((package("core", "src/core"),))
    data = facts({"core.a": "src/core/a.py", "core.b": "src/core/b.py"}, rings={"core.a": "domain", "core.b": None})
    assert found(catalog, data, Rule.PACKAGE_SPANS_RINGS) == []


def two_packages(depends_on: tuple[str, ...] = ()) -> CatalogDocument:
    return CatalogDocument((package("app", "src/app", depends_on=depends_on), package("lib", "src/lib")))


def test_importing_a_module_of_an_undeclared_package_names_module_and_both_packages() -> None:
    data = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"}, imports={"app.a": ("lib.b",)})
    [violation] = found(two_packages(), data, Rule.PACKAGE_UNDECLARED_DEPENDENCY)
    assert violation.subject == "app.a"
    assert "lib.b" in violation.detail and "app" in violation.detail and "lib" in violation.detail


def test_a_declared_dependency_is_allowed() -> None:
    data = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"}, imports={"app.a": ("lib.b",)})
    assert found(two_packages(("lib",)), data, Rule.PACKAGE_UNDECLARED_DEPENDENCY) == []


def test_imports_inside_a_package_and_into_unowned_modules_are_not_dependencies() -> None:
    data = facts(
        {"app.a": "src/app/a.py", "app.c": "src/app/c.py", "stray.d": "src/stray/d.py"},
        imports={"app.a": ("app.c", "stray.d")},
    )
    assert found(two_packages(), data, Rule.PACKAGE_UNDECLARED_DEPENDENCY) == []


def test_one_finding_per_importing_module_and_target_package() -> None:
    data = facts(
        {"app.a": "src/app/a.py", "lib.b": "src/lib/b.py", "lib.c": "src/lib/c.py"},
        imports={"app.a": ("lib.c", "lib.b")},
    )
    [violation] = found(two_packages(), data, Rule.PACKAGE_UNDECLARED_DEPENDENCY)
    assert "lib.b" in violation.detail  # the alphabetically first target is the stable example


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/*.sql", "src/a.sql", True),
        ("src/*.sql", "src/x/a.sql", False),
        ("src/**/*.sql", "src/a.sql", True),
        ("src/**/*.sql", "src/x/y/a.sql", True),
        ("**/templates/*.html", "pkg/templates/a.html", True),
        ("**/templates/*.html", "templates/a.html", True),
        ("data/**", "data/x/y.json", True),
        ("data/**", "data", False),
        ("a?.txt", "ab.txt", True),
        ("a?.txt", "a/.txt", False),
    ],
)
def test_glob_match_lets_double_star_span_folders_and_single_star_stay_in_one(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def catalog_data(**extra: Any) -> dict[str, Any]:
    return {
        "packages": [{
            "id": "core", "name": "Core", "kind": "library", "repository": "r", "root": "src", "summary": "s",
            "paths": [{"path": "src/core", "role": "source"}],
        }],
        **extra,
    }


def test_parse_catalog_reads_ordered_asset_rules() -> None:
    document = parse_catalog(catalog_data(assets=[
        {"pattern": "src/**/*.html", "package": "core"}, {"pattern": "docs/**", "package": "core"},
    ]))
    assert [(rule.pattern, rule.package) for rule in document.assets] == [("src/**/*.html", "core"), ("docs/**", "core")]


@pytest.mark.parametrize(
    "assets",
    [
        [{"pattern": "src/**", "package": "missing"}],
        [{"pattern": "/abs/**", "package": "core"}],
        [{"pattern": "../up/**", "package": "core"}],
        [{"pattern": "a\\b", "package": "core"}],
        [{"package": "core"}],
        [{"pattern": "src/**"}],
        ["not a table"],
    ],
)
def test_parse_catalog_rejects_malformed_asset_rules(assets: list[Any]) -> None:
    with pytest.raises(CatalogError):
        parse_catalog(catalog_data(assets=assets))


def test_parse_catalog_rejects_duplicate_ids_and_an_empty_catalog() -> None:
    data = catalog_data()
    data["packages"].append(dict(data["packages"][0]))
    with pytest.raises(CatalogError, match="unique"):
        parse_catalog(data)
    with pytest.raises(CatalogError, match="one or more"):
        parse_catalog({})
