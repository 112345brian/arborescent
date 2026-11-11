"""#52: every module and asset belongs to exactly one package. The pure rules, then the glob and catalog parsing."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import pytest

from arborescent.domain import INFORMATIONAL_RULES, Rule, Violation
from arborescent.package_assignment import (
    RepositoryFacts,
    assignment_violations,
    owners_of,
)
from arborescent.package_fields import glob_match, within
from arborescent.package_model import (
    ROOT_OWNER,
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


def test_an_asset_given_to_the_workspace_root_is_owned_not_unowned() -> None:
    catalog = CatalogDocument((package("core", "src/core"),), (AssetRule("LICENSE", ROOT_OWNER), AssetRule(".github/**", ROOT_OWNER)))
    data = facts(assets=("LICENSE", ".github/workflows/ci.yml", "README.md"))
    [violation] = found(catalog, data, Rule.PACKAGE_UNOWNED_ASSET)
    assert violation.subject == "README.md"
    assert not found(catalog, data, Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET)


def test_an_asset_inside_a_package_path_and_given_to_the_root_is_doubly_assigned() -> None:
    catalog = CatalogDocument((package("core", "src/core"),), (AssetRule("src/core/**/*.md", ROOT_OWNER),))
    [violation] = found(catalog, facts(assets=("src/core/NOTES.md",)), Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET)
    assert "@root" in violation.detail and "core" in violation.detail


def test_a_root_asset_is_never_a_package_owner_of_a_module() -> None:
    catalog = CatalogDocument((package("core", "src/core"),), (AssetRule("**", ROOT_OWNER),))
    assert owners_of(catalog, "src/core/a.py") == ("core",)
    assert owners_of(catalog, "LICENSE", asset=True) == (ROOT_OWNER,)


def test_asset_globs_are_first_match_wins_so_they_never_double_claim() -> None:
    catalog = CatalogDocument(
        (package("a", "src/a"), package("b", "src/b")),
        (AssetRule("data/**", "a"), AssetRule("data/**/*.sql", "b")),
    )
    data = facts(assets=("data/x/q.sql",))
    assert found(catalog, data, Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET) == []


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


def test_every_undeclared_import_is_reported_not_one_per_target_package() -> None:
    """import-linter reports every offending edge; stopping at the first per package pair hid most of a cut list."""
    data = facts(
        {"app.a": "src/app/a.py", "lib.b": "src/lib/b.py", "lib.c": "src/lib/c.py"},
        imports={"app.a": ("lib.c", "lib.b")},
    )
    violations = found(two_packages(), data, Rule.PACKAGE_UNDECLARED_DEPENDENCY)
    assert [v.subject for v in violations] == ["app.a", "app.a"]
    assert ["lib.b" in v.detail for v in violations] == [True, False] and "lib.c" in violations[1].detail  # sorted by target


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


def test_parse_catalog_accepts_the_root_owner_for_assets_but_not_as_a_package_id() -> None:
    document = parse_catalog(catalog_data(assets=[{"pattern": "LICENSE", "package": "@root"}]))
    assert [(rule.pattern, rule.package) for rule in document.assets] == [("LICENSE", ROOT_OWNER)]


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


def test_packages_that_depend_on_each_other_are_a_cycle() -> None:
    catalog = CatalogDocument((package("app", "src/app", depends_on=("lib",)), package("lib", "src/lib", depends_on=("app",))))
    [violation] = found(catalog, facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"}), Rule.PACKAGE_DEPENDENCY_CYCLE)
    assert violation.subject in {"app", "lib"}
    assert Rule.PACKAGE_DEPENDENCY_CYCLE not in INFORMATIONAL_RULES  # it fails the check and the modularity certificate


def test_a_longer_cycle_and_a_self_dependency_are_found() -> None:
    three = CatalogDocument((
        package("a", "src/a", depends_on=("b",)), package("b", "src/b", depends_on=("c",)), package("c", "src/c", depends_on=("a",)),
    ))
    assert found(three, facts({}), Rule.PACKAGE_DEPENDENCY_CYCLE)
    assert found(CatalogDocument((package("a", "src/a", depends_on=("a",)),)), facts({}), Rule.PACKAGE_DEPENDENCY_CYCLE)


def test_a_chain_and_a_diamond_are_not_cycles() -> None:
    diamond = CatalogDocument((
        package("a", "src/a", depends_on=("b", "c")), package("b", "src/b", depends_on=("d",)),
        package("c", "src/c", depends_on=("d",)), package("d", "src/d"),
    ))
    assert found(diamond, facts({}), Rule.PACKAGE_DEPENDENCY_CYCLE) == []


def test_an_unknown_dependency_is_not_reported_as_a_cycle() -> None:
    assert found(CatalogDocument((package("a", "src/a", depends_on=("ghost",)),)), facts({}), Rule.PACKAGE_DEPENDENCY_CYCLE) == []


def test_a_declared_dependency_nobody_imports_is_information() -> None:
    data = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"})
    [violation] = found(two_packages(("lib",)), data, Rule.PACKAGE_UNUSED_DEPENDENCY)
    assert violation.subject == "app" and "lib" in violation.detail
    assert Rule.PACKAGE_UNUSED_DEPENDENCY in INFORMATIONAL_RULES


def test_a_used_declared_dependency_is_not_unused() -> None:
    data = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"}, imports={"app.a": ("lib.b",)})
    assert found(two_packages(("lib",)), data, Rule.PACKAGE_UNUSED_DEPENDENCY) == []


def test_one_package_owning_every_module_is_information() -> None:
    catalog = CatalogDocument((package("all", "src"),))
    [violation] = found(catalog, facts({"a.x": "src/a/x.py", "b.y": "src/b/y.py"}), Rule.PACKAGE_OWNS_EVERYTHING)
    assert violation.subject == "all" and "2 modules" in violation.detail
    assert Rule.PACKAGE_OWNS_EVERYTHING in INFORMATIONAL_RULES


def test_owning_everything_also_applies_when_other_declared_packages_own_nothing() -> None:
    catalog = CatalogDocument((package("all", "src"), package("empty", "elsewhere")))
    assert found(catalog, facts({"a.x": "src/a/x.py", "b.y": "src/b/y.py"}), Rule.PACKAGE_OWNS_EVERYTHING)


def test_a_real_partition_or_a_single_module_does_not_own_everything() -> None:
    data = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"})
    assert found(two_packages(), data, Rule.PACKAGE_OWNS_EVERYTHING) == []
    assert found(CatalogDocument((package("all", "src"),)), facts({"a.x": "src/a/x.py"}), Rule.PACKAGE_OWNS_EVERYTHING) == []
    unassigned = facts({"a.x": "src/a/x.py", "b.y": "elsewhere/y.py"})
    assert found(CatalogDocument((package("all", "src"),)), unassigned, Rule.PACKAGE_OWNS_EVERYTHING) == []


def test_a_package_nothing_depends_on_or_imports_is_information() -> None:
    catalog = CatalogDocument((package("app", "src/app", depends_on=("lib",)), package("lib", "src/lib"), package("orphan", "src/orphan")))
    data = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py", "orphan.c": "src/orphan/c.py"}, imports={"app.a": ("lib.b",)})
    [violation] = found(catalog, data, Rule.PACKAGE_NEVER_DEPENDED_ON)
    assert violation.subject == "orphan" and Rule.PACKAGE_NEVER_DEPENDED_ON in INFORMATIONAL_RULES


def test_a_consumer_a_declared_or_imported_package_and_a_single_package_are_not_orphans() -> None:
    declared = two_packages(("lib",))
    assert [v.subject for v in found(declared, facts({}), Rule.PACKAGE_NEVER_DEPENDED_ON)] == []
    imported = facts({"app.a": "src/app/a.py", "lib.b": "src/lib/b.py"}, imports={"app.a": ("lib.b",)})
    assert found(two_packages(), imported, Rule.PACKAGE_NEVER_DEPENDED_ON) == []
    assert found(CatalogDocument((package("all", "src"),)), facts({}), Rule.PACKAGE_NEVER_DEPENDED_ON) == []


def _reference_owners(catalog: CatalogDocument, path: str) -> tuple[str, ...]:
    """The definition `owners_of` replaced: scan every package path with `within`. Kept here so the index cannot drift from it."""
    target = PurePosixPath(path)
    return tuple(sorted({p.id for p in catalog.packages if any(within(target, PurePosixPath(i.path)) for i in p.paths)}))


def test_the_indexed_owners_of_agrees_with_scanning_every_path() -> None:
    """`owners_of` answers from a prefix index (the scan was files x catalog entries and dominated every warm run). The answers must
    not change for any shape of claim: the workspace root, a folder, a single file, overlapping packages, a trailing slash."""
    catalog = CatalogDocument(
        (
            package("root", "."),
            package("core", "src/core", "src/shared/util.py"),
            package("app", "src/app/", "src/core/extra.py"),
            package("deep", "src/app/plugins/a/b"),
            package("sibling", "src/core_extras"),
        )
    )
    paths = [
        "README.md", "src", "src/core", "src/core/x.py", "src/core/sub/y.py", "src/core_extras/z.py", "src/core_extra",
        "src/shared/util.py", "src/shared/util.pyc", "src/app/m.py", "src/app/plugins/a/b/c.py", "src/app/plugins/a/c.py",
        "src/core/extra.py", "other/src/core/x.py", "", ".",
    ]
    for path in paths:
        assert owners_of(catalog, path) == _reference_owners(catalog, path), path


def test_owners_of_follows_a_catalog_that_changes_between_calls() -> None:
    """The index is built per catalog object; a different catalog must never be answered from another's index."""
    first = CatalogDocument((package("one", "src/a"),))
    second = CatalogDocument((package("two", "src/a"), package("three", "src/b")))
    assert owners_of(first, "src/a/m.py") == ("one",)
    assert owners_of(second, "src/a/m.py") == ("two",)
    assert owners_of(first, "src/a/m.py") == ("one",)
    assert owners_of(second, "src/b/m.py") == ("three",)
