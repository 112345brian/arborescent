"""#53: the package assignment compiled into the engines' own config, so the cut list is what import-linter and tach report."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files
from test_package_gaps import CATALOG, policy_with_catalog, repo

import arborescent.engine as engine
import arborescent.package_contracts as package_contracts
from arborescent.adapters import FsScanner, LocalFileSystem, SubprocessToolRunner
from arborescent.config import policy_from_mapping
from arborescent.domain import Policy, Rule
from arborescent.engine import drift, run_own, write_generated
from arborescent.generate import (
    IMPORT_LINTER_FILE,
    TACH_FILE,
    generate,
    import_linter,
    tach,
)
from arborescent.package_assignment import RepositoryFacts, assignment_violations
from arborescent.package_contracts import PackageLayout, layout_of
from arborescent.package_gaps import load_package_context, package_layout
from arborescent.package_model import CatalogError
from arborescent.package_parse import parse_catalog
from arborescent.package_preservation import preservation_violations
from arborescent.selftest import plan, selftest
from arborescent.testing import FakeToolRunner

FS = LocalFileSystem()
# core exposes only app.domain; app.ports is private to it.
PUBLIC_CATALOG = CATALOG.replace('summary = "domain and ports"', 'summary = "domain and ports"\npublic = ["app.domain"]')
PRIVATE_IMPORT = (Rule.PACKAGE_PRIVATE_IMPORT, "app.adapters")  # adapters does `from . import domain, ports`


def catalog(text: str = PUBLIC_CATALOG):
    return parse_catalog(tomllib.loads(text))


def layout(text: str = PUBLIC_CATALOG) -> PackageLayout:
    modules = {"app": "app/__init__.py", "app.domain": "app/domain.py", "app.ports": "app/ports.py",
               "app.adapters": "app/adapters.py", "app.main": "app/main.py"}
    return layout_of(catalog(text), modules)


def contracts(files: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    parsed = tomllib.loads(files[IMPORT_LINTER_FILE])["tool"]["importlinter"]["contracts"]
    return {c["name"]: c for c in parsed}


NAMES = ["app", "app.domain", "app.ports", "app.adapters", "app.main"]


def generated(packages: PackageLayout | None, policy: Policy | None = None) -> dict[str, str]:
    policy = policy or mini_policy()
    return {IMPORT_LINTER_FILE: import_linter(policy, NAMES, packages=packages), TACH_FILE: tach(policy, NAMES, packages=packages)}


# --- the catalog's `public` ------------------------------------------------------------------------------------------


def test_public_is_parsed_as_declared_by_the_catalog_and_empty_when_unset() -> None:
    assert {p.id: p.public for p in catalog(CATALOG).packages} == {"core": (), "shell": ()}
    assert {p.id: p.public for p in catalog().packages} == {"core": ("app.domain",), "shell": ()}


def test_public_must_be_a_list_of_strings() -> None:
    with pytest.raises(CatalogError, match="public"):
        catalog(CATALOG.replace('summary = "domain and ports"', 'summary = "x"\npublic = "app.domain"'))


# --- the layout and its one rule -------------------------------------------------------------------------------------


def test_the_layout_holds_only_modules_with_exactly_one_owner() -> None:
    assert layout().owner == {"app": "core", "app.domain": "core", "app.ports": "core", "app.adapters": "shell", "app.main": "shell"}
    twice = PUBLIC_CATALOG + '\n[[packages]]\nid = "dup"\nname = "D"\nkind = "library"\nrepository = "r"\nroot = "app"\n' \
        'summary = "s"\n[[packages.paths]]\npath = "app/domain.py"\nrole = "source"\n'
    assert "app.domain" not in layout(twice).owner


@pytest.mark.parametrize(("importer", "imported", "allowed"), [
    ("app.adapters", "app.domain", True),    # declared dependency, public module
    ("app.adapters", "app.ports", False),    # declared dependency, private module
    ("app.main", "app.adapters", True),      # same package
    ("app.domain", "app.adapters", False),   # core does not declare shell
    ("app.domain", "app.unknown", True),     # a module nobody owns is a gap reported elsewhere, not an edge to forbid
    ("app.unknown", "app.adapters", True),
])
def test_allows(importer: str, imported: str, allowed: bool) -> None:
    assert layout().allows(importer, imported) is allowed


def test_a_package_without_a_public_list_is_open_to_those_that_declare_it() -> None:
    """The semantics of #55: no declared surface means nothing of the package is private."""
    assert layout(CATALOG).allows("app.adapters", "app.ports")
    assert layout(CATALOG.replace('summary = "domain and ports"', 'summary = "x"\npublic = []')).allows("app.adapters", "app.ports")


def test_the_own_rules_and_the_layout_never_disagree() -> None:
    """The engines are compiled from ``allows``; the own findings (#52 and #55) must flag exactly the edges it refuses."""
    modules = layout().owner
    paths = {m: m.replace(".", "/") + ".py" for m in modules} | {"app": "app/__init__.py"}
    for importer in modules:
        for imported in modules:
            if importer == imported:
                continue
            facts = RepositoryFacts(paths, {}, {importer: (imported,)}, ())
            found = assignment_violations(catalog(), facts) + preservation_violations(catalog(), facts, [])
            flagged = [v for v in found if v.rule in {Rule.PACKAGE_UNDECLARED_DEPENDENCY, Rule.PACKAGE_PRIVATE_IMPORT}]
            assert bool(flagged) is (not layout().allows(importer, imported)), (importer, imported)


def test_a_private_module_of_a_declared_dependency_is_reported_by_the_own_check_too(tmp_path: Path) -> None:
    repo(tmp_path, {"packages.toml": PUBLIC_CATALOG})
    found, _ = run_own(policy_with_catalog(), tmp_path, services())
    mine = [(v.rule, v.subject) for v in found if v.rule in {Rule.PACKAGE_PRIVATE_IMPORT, Rule.PACKAGE_UNDECLARED_DEPENDENCY}]
    assert mine == [PRIVATE_IMPORT]


# --- the generated import-linter contracts ---------------------------------------------------------------------------


def test_nothing_changes_for_a_repository_without_a_catalog() -> None:
    assert generated(None) == {k: v for k, v in generate(mini_policy(), NAMES, []).items() if k in {IMPORT_LINTER_FILE, TACH_FILE}}
    assert not any(name.startswith("package ") for name in contracts(generated(None)))


def test_each_package_gets_a_contract_forbidding_everything_it_may_not_import() -> None:
    found = contracts(generated(layout()))
    core = found["package core imports only declared packages' public modules"]
    assert core["source_modules"] == ["app.domain", "app.ports"]
    assert core["forbidden_modules"] == ["app.adapters", "app.main"]
    shell = found["package shell imports only declared packages' public modules"]
    assert shell["source_modules"] == ["app.adapters", "app.main"]
    assert shell["forbidden_modules"] == ["app.ports"]  # core is declared, but only app.domain is public
    assert shell["allow_indirect_imports"] is True      # the cut list is direct edges


def test_a_package_that_is_wholly_allowed_everywhere_gets_no_boundary_contract() -> None:
    single = "[[packages]]\nid = \"all\"\nname = \"A\"\nkind = \"library\"\nrepository = \"r\"\nroot = \"app\"\nsummary = \"s\"\n" \
        + "".join(f'[[packages.paths]]\npath = "app/{p}"\nrole = "source"\n' for p in ("__init__.py", "domain.py", "ports.py", "adapters.py", "main.py"))
    assert not any(n.startswith("package ") and "public modules" in n for n in contracts(generated(layout(single))))


def test_a_package_forbids_the_third_party_names_none_of_its_rings_list() -> None:
    found = contracts(generated(layout()))
    core = found["package core imports no I/O or third-party package its rings do not list"]
    shell = found["package shell imports no I/O or third-party package its rings do not list"]
    assert "os" in core["forbidden_modules"] and "os" not in shell["forbidden_modules"]  # adapters list os
    assert "subprocess" in core["forbidden_modules"] and "subprocess" in shell["forbidden_modules"]  # no ring lists it


def test_the_external_contract_is_skipped_when_a_ring_of_the_package_allows_everything() -> None:
    open_rings = mini_policy(ring={
        "domain": {"external": ["@pure"]}, "ports": {"may_import": ["domain"], "external": ["@pure"]},
        "adapters": {"may_import": ["domain", "ports"], "external": ["*"]},
        "composition": {"may_import": ["*"], "external": ["@pure", "sys"]},
    })
    found = contracts(generated(layout(), open_rings))
    assert "package shell imports no I/O or third-party package its rings do not list" not in found
    assert "package core imports no I/O or third-party package its rings do not list" in found


# --- tach ------------------------------------------------------------------------------------------------------------


def test_tach_depends_on_follows_the_package_boundary_and_its_public_modules() -> None:
    modules = {m["path"]: [d["path"] for d in m["depends_on"]] for m in tomllib.loads(generated(layout())[TACH_FILE])["modules"]}
    assert modules["app.adapters"] == ["app.domain"]            # ring allows domain and ports; ports is private to core
    assert modules["app.main"] == ["app.adapters", "app.domain"]  # shell reaches core only through its public module
    assert modules["app.ports"] == ["app.domain"]
    assert modules["app.domain"] == []


def test_tach_stays_layer_only_without_a_catalog() -> None:
    assert "depends_on = [" not in generated(None)[TACH_FILE]


def test_a_catalog_never_widens_what_the_rings_allow() -> None:
    one = "[[packages]]\nid = \"all\"\nname = \"A\"\nkind = \"library\"\nrepository = \"r\"\nroot = \"app\"\nsummary = \"s\"\n" \
        + "".join(f'[[packages.paths]]\npath = "app/{p}"\nrole = "source"\n' for p in ("__init__.py", "domain.py", "ports.py", "adapters.py", "main.py"))
    modules = {m["path"]: [d["path"] for d in m["depends_on"]] for m in tomllib.loads(generated(layout(one))[TACH_FILE])["modules"]}
    assert "app.adapters" not in modules["app.domain"]  # domain still may not import adapters


# --- scopes ----------------------------------------------------------------------------------------------------------


def test_packages_compose_with_scopes() -> None:
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {"sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"], "rings": ["domain"],
                      "classify": {"alpha.core": "domain", "alpha.more": "domain"}, "ring": {"domain": {"external": ["*"]}}},
        },
    })
    owner = layout_of(catalog("""
[[packages]]
id = "one"
name = "One"
kind = "library"
repository = "r"
root = "libs"
summary = "s"
[[packages.paths]]
path = "libs/alpha/src/alpha/core.py"
role = "source"
[[packages]]
id = "two"
name = "Two"
kind = "library"
repository = "r"
root = "libs"
summary = "s"
[[packages.paths]]
path = "libs/alpha/src/alpha/more.py"
role = "source"
"""), {"alpha.core": "libs/alpha/src/alpha/core.py", "alpha.more": "libs/alpha/src/alpha/more.py"})
    names, owners = ["alpha", "alpha.core", "alpha.more"], {"alpha.core": "alpha", "alpha.more": "alpha"}
    files = {IMPORT_LINTER_FILE: import_linter(policy, names, scope_by_module=owners, packages=owner),
             TACH_FILE: tach(policy, names, scope_by_module=owners, packages=owner)}
    found = contracts(files)
    assert found["package one imports only declared packages' public modules"]["forbidden_modules"] == ["alpha.more"]
    scoped = {m["path"]: [d["path"] for d in m["depends_on"]] for m in tomllib.loads(files[TACH_FILE])["modules"]}
    assert scoped == {"alpha.core": [], "alpha.more": []}


# --- wiring: generate and check read the same layout -----------------------------------------------------------------


def test_the_layout_is_none_without_a_catalog(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    policy = mini_policy()
    assert package_layout(policy, tmp_path, FS, FsScanner().scan(policy, tmp_path)) is None


def test_generate_writes_package_contracts_and_check_reports_a_stale_file(tmp_path: Path) -> None:
    repo(tmp_path, {"packages.toml": PUBLIC_CATALOG})
    policy = policy_with_catalog()
    assert "package shell imports only declared" in "".join(
        (tmp_path / rel).read_text(encoding="utf-8") for rel in write_generated(policy, tmp_path, services()) if rel.endswith("importlinter.toml"))
    scan = FsScanner().scan(policy, tmp_path)
    assert drift(FS, policy, tmp_path, scan, []) == []
    write_files(tmp_path, {"packages.toml": PUBLIC_CATALOG.replace('public = ["app.domain"]', 'public = ["app.domain", "app.ports"]')})
    assert any("arborescent-importlinter.toml: differs from the policy" in line for line in drift(FS, policy, tmp_path, scan, []))


def test_engines_are_the_only_judge_of_the_edges_check_does_not_import_a_graph_library() -> None:
    source = Path(package_contracts.__file__).read_text(encoding="utf-8")
    assert "grimp" not in source and "import networkx" not in source


# --- the real engines report the cut list ----------------------------------------------------------------------------


def _project(root: Path, files: dict[str, str] | None = None) -> None:
    repo(root, {"packages.toml": PUBLIC_CATALOG, **(files or {})})
    policy = policy_with_catalog()
    _ = write_generated(policy, root, services(FakeToolRunner()))
    write_files(root, {"pyproject.toml": "[project]\nname = \"x\"\ndependencies = []\n"})


@pytest.mark.skipif(shutil.which("lint-imports") is None, reason="import-linter not installed")
def test_import_linter_lists_the_private_import_as_the_cut_list(tmp_path: Path) -> None:
    _project(tmp_path)
    broken = SubprocessToolRunner().run(["lint-imports", "--config", "arborescent-importlinter.toml"], tmp_path)
    assert broken.returncode != 0
    assert "package shell imports only declared packages' public modules" in broken.stdout
    assert "app.adapters is not allowed to import app.ports" in broken.stdout
    assert "app.adapters is not allowed to import app.domain" not in broken.stdout


@pytest.mark.skipif(shutil.which("lint-imports") is None, reason="import-linter not installed")
def test_import_linter_is_silent_once_the_cut_is_made(tmp_path: Path) -> None:
    _project(tmp_path, {"app/adapters.py": MINI_FILES["app/adapters.py"].replace("from . import domain, ports", "from . import domain")
                        .replace(", ports.PORT_VALUE", "")})
    ok = SubprocessToolRunner().run(["lint-imports", "--config", "arborescent-importlinter.toml"], tmp_path)
    assert ok.returncode == 0, ok.stdout


@pytest.mark.skipif(shutil.which("lint-imports") is None, reason="import-linter not installed")
def test_import_linter_reports_an_undeclared_package(tmp_path: Path) -> None:
    _project(tmp_path, {"app/domain.py": "from . import adapters\n"})
    broken = SubprocessToolRunner().run(["lint-imports", "--config", "arborescent-importlinter.toml"], tmp_path)
    assert "package core imports only declared packages' public modules" in broken.stdout
    assert "app.domain is not allowed to import app.adapters" in broken.stdout


@pytest.mark.skipif(shutil.which("tach") is None, reason="tach not installed")
def test_tach_reports_the_private_import(tmp_path: Path) -> None:
    _project(tmp_path)
    broken = SubprocessToolRunner().run(["tach", "check", "--dependencies"], tmp_path)
    assert broken.returncode != 0
    assert "app.adapters" in broken.stdout + broken.stderr and "app.ports" in broken.stdout + broken.stderr


# --- the selftest probes ---------------------------------------------------------------------------------------------


def test_the_private_import_has_a_selftest_probe_when_a_package_keeps_part_of_itself_private(tmp_path: Path) -> None:
    repo(tmp_path, {"packages.toml": PUBLIC_CATALOG})
    policy = policy_with_catalog()
    scan = FsScanner().scan(policy, tmp_path)
    assert Rule.PACKAGE_PRIVATE_IMPORT in {m.rule for m in plan(policy, scan, load_package_context(policy, tmp_path, FS, scan))[0]}


def test_the_private_import_probe_says_why_it_is_skipped_when_every_module_is_public(tmp_path: Path) -> None:
    repo(tmp_path)  # no `public` anywhere
    policy = policy_with_catalog()
    scan = FsScanner().scan(policy, tmp_path)
    mutations, skipped = plan(policy, scan, load_package_context(policy, tmp_path, FS, scan))
    assert Rule.PACKAGE_PRIVATE_IMPORT not in {m.rule for m in mutations}
    assert any(line.startswith(Rule.PACKAGE_PRIVATE_IMPORT) for line in skipped)


def test_selftest_kills_the_private_import_probe(tmp_path: Path) -> None:
    repo(tmp_path, {"packages.toml": PUBLIC_CATALOG.replace('depends_on = ["core"]', 'depends_on = ["core"]')})
    write_files(tmp_path, {"app/adapters.py": MINI_FILES["app/adapters.py"].replace("from . import domain, ports", "from . import domain")
                           .replace(", ports.PORT_VALUE", "")})  # start with no private import, so the probe's is the new one
    report = selftest(policy_with_catalog(), tmp_path, services())
    assert report.survived == []
    assert any(entry.startswith(f"[{Rule.PACKAGE_PRIVATE_IMPORT}]") for entry in report.killed)


def test_selftest_survives_when_the_private_import_rule_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard against rule removal: with the gap check stubbed out the probe must be reported as a survivor."""
    repo(tmp_path, {"packages.toml": PUBLIC_CATALOG})
    write_files(tmp_path, {"app/adapters.py": MINI_FILES["app/adapters.py"].replace("from . import domain, ports", "from . import domain")
                           .replace(", ports.PORT_VALUE", "")})
    monkeypatch.setattr(engine, "package_gap_violations", lambda *args, **kwargs: [])
    report = selftest(policy_with_catalog(), tmp_path, services())
    assert any(entry.startswith(f"[{Rule.PACKAGE_PRIVATE_IMPORT}]") for entry in report.survived)
