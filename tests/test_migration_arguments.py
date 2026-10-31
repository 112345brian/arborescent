"""#54: the tool-neutral migration arguments of a package, built from the catalog and the repository facts (pure)."""

from __future__ import annotations

import tomllib
from collections.abc import Callable

import pytest

from arborescent.domain import Rule
from arborescent.migration_arguments import (
    GapCode,
    MigrationArguments,
    build_migration_arguments,
)
from arborescent.package_assignment import RepositoryFacts
from arborescent.package_model import CatalogError
from arborescent.package_parse import parse_catalog

CATALOG = """
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/mono"
root = "src/app/core"
summary = "domain and ports"
public = ["app.core.api"]
externals = ["pydantic"]
[packages.home]
kind = "repository"
repository = "example/core"
[[packages.module_renames]]
from = "app.core"
to = "core"
[[packages.paths]]
path = "src/app/core/api.py"
role = "source"
[[packages.paths]]
path = "src/app/core/internal.py"
role = "source"
[[packages.paths]]
path = "src/app/core/schema"
role = "assets"
destination = "data/schema"

[[packages]]
id = "web"
name = "Web"
kind = "application"
repository = "example/mono"
root = "src/app/web"
summary = "http layer"
depends_on = ["core"]
public = []
externals = ["flask"]
[packages.home]
kind = "workspace-member"
path = "packages/web"
[[packages.paths]]
path = "src/app/web"
role = "source"

[[assets]]
pattern = "src/app/**/*.sql"
package = "core"
"""

FACTS = RepositoryFacts(
    modules={
        "app.core.api": "src/app/core/api.py",
        "app.core.internal": "src/app/core/internal.py",
        "app.web.views": "src/app/web/views.py",
        "app.web.routes": "src/app/web/routes.py",
    },
    rings={"app.core.api": "domain", "app.core.internal": "domain", "app.web.views": "adapters", "app.web.routes": "composition"},
    imports={"app.web.views": ("app.core.api",), "app.web.routes": ("app.core.internal", "app.web.views")},
    assets=("src/app/core/schema/user.json", "src/app/web/page.html"),
    externals={"app.core.api": ("pydantic",), "app.web.views": ("flask",)},
)


def build(catalog: str = CATALOG, facts: RepositoryFacts = FACTS) -> MigrationArguments:
    return build_migration_arguments(parse_catalog(tomllib.loads(catalog)), facts)


def package(arguments: MigrationArguments, package_id: str):
    return next(item for item in arguments.packages if item.id == package_id)


def codes(arguments: MigrationArguments) -> set[tuple[str, str]]:
    return {(gap.code, gap.subject) for gap in arguments.gaps}


def test_identity_and_home_are_carried_for_both_outcomes() -> None:
    arguments = build()
    core, web = package(arguments, "core"), package(arguments, "web")
    assert (core.name, core.kind, core.summary) == ("Core", "library", "domain and ports")
    assert (core.home.kind, core.home.repository, core.home.path) == ("repository", "example/core", None)  # type: ignore[union-attr]
    assert (web.home.kind, web.home.repository, web.home.path) == ("workspace-member", None, "packages/web")  # type: ignore[union-attr]


def test_owned_modules_and_assets_are_repository_relative_with_their_target_paths() -> None:
    core = package(build(), "core")
    assert [(m.module, m.path, m.target_path) for m in core.modules] == [
        ("app.core.api", "src/app/core/api.py", "api.py"),
        ("app.core.internal", "src/app/core/internal.py", "internal.py"),
    ]
    assert [(a.path, a.target_path) for a in core.assets] == [("src/app/core/schema/user.json", "data/schema/user.json")]
    web = package(build(), "web")
    assert [(a.path, a.target_path) for a in web.assets] == [("src/app/web/page.html", "packages/web/page.html")]


def test_a_workspace_member_lands_below_its_home_path() -> None:
    web = package(build(), "web")
    assert [(m.path, m.target_path) for m in web.modules] == [
        ("src/app/web/routes.py", "packages/web/routes.py"),
        ("src/app/web/views.py", "packages/web/views.py"),
    ]
    assert [(r.source, r.target) for r in web.path_renames] == [("src/app/web", "packages/web")]


def test_path_renames_follow_destinations_and_roots() -> None:
    core = package(build(), "core")
    assert [(r.source, r.target) for r in core.path_renames] == [
        ("src/app/core/api.py", "api.py"),
        ("src/app/core/internal.py", "internal.py"),
        ("src/app/core/schema", "data/schema"),
    ]


def test_the_module_name_map_applies_the_longest_declared_prefix() -> None:
    core = package(build(), "core")
    assert [(m.module, m.target_module) for m in core.modules] == [("app.core.api", "core.api"), ("app.core.internal", "core.internal")]
    web = package(build(), "web")
    assert [(m.module, m.target_module) for m in web.modules] == [("app.web.routes", "app.web.routes"), ("app.web.views", "app.web.views")]


def test_public_surface_and_the_modules_that_bypass_it() -> None:
    core = package(build(), "core")
    assert core.public_surface == ("app.core.api",)
    assert [(b.module, b.package, b.imports) for b in core.bypasses] == [("app.web.routes", "web", "app.core.internal")]


def test_declared_dependencies_first_party_and_external() -> None:
    arguments = build()
    assert package(arguments, "web").depends_on == ("core",)
    assert package(arguments, "core").externals == ("pydantic",)
    edges = {(e.source, e.target, e.kind) for e in arguments.edges}
    assert edges == {("web", "core", "depends_on"), ("web", "core", "imports")}


def test_a_complete_catalog_has_no_blocking_gap() -> None:
    arguments = build()
    assert [(g.code, g.subject) for g in arguments.gaps if g.severity == "error"] == []
    assert arguments.ready


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda c: c.replace('[packages.home]\nkind = "repository"\nrepository = "example/core"\n', ""), ("package-undeclared-home", "core")),
        (lambda c: c.replace('public = ["app.core.api"]\n', ""), ("package-undeclared-public-surface", "core")),
        (lambda c: c.replace('externals = ["pydantic"]\n', ""), ("package-undeclared-external", "core")),
        (lambda c: c.replace('externals = ["flask"]\n', ""), ("package-undeclared-external", "web")),
        (lambda c: c.replace('to = "core"\n', 'to = "core"\n[[packages.module_renames]]\nfrom = "nothing.here"\nto = "x"\n'), ("package-unused-module-rename", "nothing.here")),
        (lambda c: c.replace('to = "core"\n', 'to = "core"\n[[packages.module_renames]]\nfrom = "app.core.internal"\nto = "core.api"\n'), ("package-module-rename-collision", "core.api")),
    ],
)
def test_each_unresolved_annotation_is_a_named_gap(edit: Callable[[str], str], expected: tuple[str, str]) -> None:
    assert expected in codes(build(edit(CATALOG)))


def test_an_owned_path_outside_the_root_without_a_destination_is_unplaceable() -> None:
    arguments = build(CATALOG.replace('root = "src/app/core"', 'root = "src/app/elsewhere"'))
    assert ("package-unplaceable-path", "src/app/core/api.py") in codes(arguments)


def test_an_asset_below_no_owned_path_is_an_unplaced_gap() -> None:
    facts = RepositoryFacts(FACTS.modules, FACTS.rings, FACTS.imports, (*FACTS.assets, "src/app/q.sql"), FACTS.externals)
    arguments = build(facts=facts)
    assert ("package-unplaced-asset", "src/app/q.sql") in codes(arguments)
    assert "src/app/q.sql" in [a.path for a in package(arguments, "core").assets]  # owned by the glob, so listed, without a target
    assert next(a for a in package(arguments, "core").assets if a.path == "src/app/q.sql").target_path is None


def test_the_assignment_findings_are_gaps_with_their_rule_id_and_ring_spans_is_information() -> None:
    facts = RepositoryFacts({**FACTS.modules, "app.stray": "src/app/stray.py"}, FACTS.rings, FACTS.imports, FACTS.assets, FACTS.externals)
    arguments = build(facts=facts)
    unassigned = [g for g in arguments.gaps if g.code == Rule.PACKAGE_UNASSIGNED_MODULE]
    assert [(g.subject, g.severity) for g in unassigned] == [("app.stray", "error")]
    spans = [g for g in arguments.gaps if g.code == Rule.PACKAGE_SPANS_RINGS]
    assert [(g.subject, g.severity) for g in spans] == [("web", "info")]


def test_the_arguments_are_deterministic() -> None:
    assert build() == build()


def test_home_is_validated_when_the_catalog_is_parsed() -> None:
    bad_kind = CATALOG.replace('kind = "workspace-member"', 'kind = "somewhere"')
    with pytest.raises(CatalogError, match="home kind"):
        build(bad_kind)
    no_path = CATALOG.replace('path = "packages/web"\n', "")
    with pytest.raises(CatalogError, match="requires a path"):
        build(no_path)
    no_repository = CATALOG.replace('repository = "example/core"\n[[packages.module_renames]]', "[[packages.module_renames]]")
    with pytest.raises(CatalogError, match="requires a repository"):
        build(no_repository)
    clash = CATALOG.replace('kind = "repository"\nrepository = "example/core"', 'kind = "workspace-member"\npath = "packages/web/inner"')
    with pytest.raises(CatalogError, match="nest"):
        build(clash)


def test_export_only_gap_codes_never_collide_with_check_rule_ids() -> None:
    assert not set(GapCode.ALL) & {str(rule) for rule in Rule}


def test_a_package_with_no_declared_surface_has_none_and_nothing_bypasses_it() -> None:
    core = package(build(CATALOG.replace('public = ["app.core.api"]\n', "")), "core")
    assert core.public_surface == ()
    assert core.bypasses == ()  # no declared surface: nothing is private (the same meaning as in package_parts, #55)
