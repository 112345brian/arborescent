"""#54: the tool-neutral migration arguments of a package, built from the catalog and the repository facts (pure)."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from arborescent.domain import Rule
from arborescent.migration_arguments import (
    GapCode,
    MigrationArguments,
    build_migration_arguments,
)
from arborescent.package_assignment import RepositoryFacts
from arborescent.package_model import CatalogError, PackageHome, is_git_url
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
history = "preserve"
[packages.release]
pin = "tag"
merge = "merge-commit"
tag_before_branch_delete = true
[packages.home]
kind = "repository"
repository = "example/core"
[[packages.module_renames]]
from = "app.core"
to = "core"
[[packages.paths]]
path = "src/app/core/api.py"
role = "source"
destination = "src/core/api.py"
[[packages.paths]]
path = "src/app/core/internal.py"
role = "source"
destination = "src/core/internal.py"
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
destination = "src/app/web"

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
        ("app.core.api", "src/app/core/api.py", "src/core/api.py"),
        ("app.core.internal", "src/app/core/internal.py", "src/core/internal.py"),
    ]
    assert [(a.path, a.target_path) for a in core.assets] == [("src/app/core/schema/user.json", "data/schema/user.json")]
    web = package(build(), "web")
    assert [(a.path, a.target_path) for a in web.assets] == [("src/app/web/page.html", "packages/web/src/app/web/page.html")]


def test_a_workspace_member_lands_below_its_home_path() -> None:
    web = package(build(), "web")
    assert [(m.path, m.target_path) for m in web.modules] == [
        ("src/app/web/routes.py", "packages/web/src/app/web/routes.py"),
        ("src/app/web/views.py", "packages/web/src/app/web/views.py"),
    ]
    assert [(r.source, r.target) for r in web.path_renames] == [("src/app/web", "packages/web/src/app/web")]


def test_path_renames_follow_destinations_and_roots() -> None:
    core = package(build(), "core")
    assert [(r.source, r.target) for r in core.path_renames] == [
        ("src/app/core/api.py", "src/core/api.py"),
        ("src/app/core/internal.py", "src/core/internal.py"),
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
    arguments = build(CATALOG.replace('root = "src/app/core"', 'root = "src/app/elsewhere"').replace('destination = "src/core/api.py"\n', ""))
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
    member_core = CATALOG.replace('history = "preserve"\n[packages.release]\npin = "tag"\nmerge = "merge-commit"\ntag_before_branch_delete = true\n', "")  # a member has no policy
    clash = member_core.replace('kind = "repository"\nrepository = "example/core"', 'kind = "workspace-member"\npath = "packages/web/inner"')
    with pytest.raises(CatalogError, match="nest"):
        build(clash)


def test_export_only_gap_codes_never_collide_with_check_rule_ids() -> None:
    assert not set(GapCode.ALL) & {str(rule) for rule in Rule}


def test_a_package_with_no_declared_surface_has_none_and_nothing_bypasses_it() -> None:
    core = package(build(CATALOG.replace('public = ["app.core.api"]\n', "")), "core")
    assert core.public_surface == ()
    assert core.bypasses == ()  # no declared surface: nothing is private (the same meaning as in package_parts, #55)


@pytest.mark.parametrize("repository", ["example/core", "https://github.com/example/core.git", "git@github.com:example/core.git"])
def test_a_repository_home_is_any_non_empty_name_kept_unchanged(repository: str) -> None:
    core = package(build(CATALOG.replace('repository = "example/core"\n[[packages.module_renames]]', f'repository = "{repository}"\n[[packages.module_renames]]')), "core")
    assert core.home is not None and core.home.repository == repository


@pytest.mark.parametrize("value", ['""', '"   "', "5", "true", "[]"])
def test_an_empty_or_non_string_home_repository_is_an_error(value: str) -> None:
    with pytest.raises(CatalogError, match="repository"):
        build(CATALOG.replace('repository = "example/core"\n[[packages.module_renames]]', f"repository = {value}\n[[packages.module_renames]]"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/example/core.git", True),
        ("https://example.org/core", True),
        ("https://host", True),
        ("ssh://git@github.com/example/core.git", True),
        ("git@github.com:example/core.git", True),
        ("example/core", False),
        ("core", False),
        ("http://github.com/example/core.git", False),  # no guessing: only https, ssh and scp-style git@
        ("ftp://host/core", False),
        ("file:///srv/core", False),
        ("github.com/example/core", False),
        ("https://", False),
        ("ssh://", False),
        ("git@github.com", False),
        ("git@:org/name", False),
        ("https://host/a b", False),
        ("", False),
    ],
)
def test_is_git_url_classifies_only_https_ssh_and_scp_style_urls(value: str, expected: bool) -> None:
    assert is_git_url(value) is expected


def test_the_home_derives_whether_its_repository_is_a_git_url_and_never_stores_it() -> None:
    assert PackageHome("repository", repository="example/core").repository_is_git_url is False
    assert PackageHome("repository", repository="git@github.com:example/core.git").repository_is_git_url is True
    assert PackageHome("workspace-member", path="packages/x").repository_is_git_url is None


RENAME_CATALOG = """
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/mono"
root = "app/core"
summary = "domain"
[packages.home]
kind = "{kind}"
{home}
{renames}
[[packages.paths]]
path = "app/core"
role = "source"
{destination}
"""
RENAME_FACTS = RepositoryFacts(
    modules={"app.core.models": "app/core/models.py", "app.core": "app/core/__init__.py", "app.core.sub.deep": "app/core/sub/deep.py"},
    rings={}, imports={}, assets=(), externals={},
)
RENAME = '[[packages.module_renames]]\nfrom = "app.core"\nto = "core"\n'
OWN_REPO = 'kind = "repository"\nrepository = "example/core"'
MEMBER = 'kind = "workspace-member"\npath = "packages/core"'


def _destination(destination: str) -> str:
    """"." means the package root lands at the target root, which is the default, so no `destination` line."""
    return "" if destination == "." else f'destination = "{destination}"'


def rename_gaps(*, destination: str, renames: str = "", home: str = OWN_REPO) -> set[tuple[str, str]]:
    kind = "workspace-member" if "path =" in home else "repository"
    text = RENAME_CATALOG.format(kind=kind, home=home.split("\n", 1)[1], renames=renames, destination=_destination(destination))
    return {(g.code, g.subject) for g in build(text, RENAME_FACTS).gaps if g.code == GapCode.MODULE_RENAME_MISMATCH}


def test_a_move_that_changes_the_import_name_without_a_rename_is_a_mismatch() -> None:
    # app/core/models.py lands at models.py: the module becomes `models`, but nothing says so and imports are not rewritten.
    assert ("package-module-rename-mismatch", "app.core.models") in rename_gaps(destination=".")


def test_a_declared_rename_that_matches_the_move_is_clean() -> None:
    assert rename_gaps(destination="src/core", renames=RENAME) == set()
    assert rename_gaps(destination="core", renames=RENAME) == set()


def test_a_rename_for_a_module_whose_path_did_not_change_is_a_mismatch() -> None:
    # The path keeps `app/core/...` but a rename claims the module is now `core.*`, so rewritten imports name nothing real.
    found = rename_gaps(destination="app/core", renames=RENAME)
    assert {subject for _, subject in found} == {"app.core", "app.core.models", "app.core.sub.deep"}


def test_a_path_move_that_keeps_the_name_needs_no_rename() -> None:
    assert rename_gaps(destination="app/core", home=OWN_REPO) == set()
    assert rename_gaps(destination="app/core", home=MEMBER) == set()
    assert rename_gaps(destination="src/app/core") == set()  # a src layout in the new repository


def test_a_workspace_member_is_measured_from_its_own_folder() -> None:
    assert rename_gaps(destination="core", renames=RENAME, home=MEMBER) == set()
    assert rename_gaps(destination="src/core", renames=RENAME, home=MEMBER) == set()
    assert ("package-module-rename-mismatch", "app.core.models") in rename_gaps(destination=".", home=MEMBER)


def test_a_module_with_no_target_path_is_left_to_the_unplaced_rules() -> None:
    facts = RepositoryFacts({"app.core.models": "app/core/models.py", "app.other": "app/other.py"}, {}, {}, (), {})
    text = RENAME_CATALOG.format(kind="repository", home='repository = "example/core"', renames="", destination=_destination("app/core"))
    assert not [g for g in build(text, facts).gaps if g.code == GapCode.MODULE_RENAME_MISMATCH and g.subject == "app.other"]


def test_the_mismatch_gap_blocks_a_migration_and_is_documented() -> None:
    text = RENAME_CATALOG.format(kind="repository", home='repository = "example/core"', renames="", destination=_destination("."))
    arguments = build(text, RENAME_FACTS)
    assert not arguments.ready
    assert GapCode.MODULE_RENAME_MISMATCH in GapCode.ALL
    assert GapCode.MODULE_RENAME_MISMATCH in Path("docs/migration-export.md").read_text(encoding="utf-8")
