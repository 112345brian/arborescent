"""#60: the derivable parts of each package's ``pyproject.toml``, written once and checked for drift, never rewritten."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from jsonschema import Draft202012Validator, ValidationError

import arborescent.engine as engine
from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.domain import INFORMATIONAL_RULES, PolicyError, Rule
from arborescent.engine import Options, run_check, run_own, write_generated
from arborescent.migration_export import migration_arguments_of
from arborescent.migration_json import SCHEMA, export_document
from arborescent.package_gaps import load_package_context
from arborescent.package_model import CatalogError
from arborescent.practices import undeclared_imports
from arborescent.selftest import plan, selftest

FS = LocalFileSystem()

LAYOUT = {
    "core/src/core/__init__.py": "",
    "core/src/core/domain.py": "from __future__ import annotations\n\nVALUE: int = 1\n",
    "shell/src/shell/__init__.py": "",
    "shell/src/shell/adapters.py": (
        "from __future__ import annotations\n\nimport bs4\n\nfrom core import domain\n\nSOUP: object = bs4\nVALUE: int = domain.VALUE\n"
    ),
}

CATALOG = """
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/mono"
root = "core"
summary = "the domain"
[packages.home]
kind = "workspace-member"
path = "core"
[[packages.paths]]
path = "core"
role = "source"

[[packages]]
id = "shell"
name = "Shell"
kind = "application"
repository = "example/mono"
root = "shell"
summary = "adapters"
depends_on = ["core"]
[packages.home]
kind = "workspace-member"
path = "shell"
[[packages.paths]]
path = "shell"
role = "source"
"""

OPTIONS = {
    "sources": ["core/src/core", "shell/src/shell"],
    "source_roots": ["core/src", "shell/src"],
    "rings": ["domain", "adapters"],
    "groups": {"pure": ["__future__", "dataclasses", "typing", "collections"]},
    "ring": {
        "domain": {"external": ["@pure", "core"]},
        "adapters": {"may_import": ["domain"], "external": ["@pure", "bs4", "core", "shell"]},
    },
    "classify": {"core": "domain", "core.domain": "domain", "shell": "adapters", "shell.adapters": "adapters"},
    "dependencies": {"bs4": "beautifulsoup4"},
    "package_catalog": "packages.toml",
    "package_pyproject": True,
}


def policy(**overrides: object):
    return mini_policy(**{**OPTIONS, **overrides})


def repo(root: Path, catalog: str = CATALOG, extra: dict[str, str] | None = None) -> None:
    write_files(root, {**LAYOUT, "packages.toml": catalog, **(extra or {})})


def load(root: Path, rel: str) -> dict[str, object]:
    return tomllib.loads((root / rel).read_text(encoding="utf-8"))


def drift(root: Path, **overrides: object) -> list[tuple[str, str]]:
    found, _ = run_own(policy(**overrides), root, services())
    return sorted((v.subject, v.detail) for v in found if v.rule == Rule.PACKAGE_PYPROJECT_DRIFT)


def test_generate_writes_a_pyproject_per_package_with_only_derived_fields_and_a_template(tmp_path: Path) -> None:
    repo(tmp_path)
    written = write_generated(policy(), tmp_path, services())
    assert {"core/pyproject.toml", "shell/pyproject.toml"} <= set(written)
    shell = load(tmp_path, "shell/pyproject.toml")
    project = shell["project"]
    assert isinstance(project, dict)
    assert project["name"] == "shell" and project["description"] == "adapters"
    assert project["dependencies"] == ["beautifulsoup4", "core"]  # third party via the mapping, then the sibling
    assert shell["tool"]["uv"]["sources"] == {"core": {"workspace": True}}  # type: ignore[index]
    assert shell["tool"]["arborescent"]["source_roots"] == ["src"]  # type: ignore[index]
    assert shell["tool"]["tach"]["source_roots"] == ["src"]  # type: ignore[index]
    assert shell["tool"]["importlinter"]["root_packages"] == ["shell"]  # type: ignore[index]
    assert "build-system" not in shell, "no build configuration is invented"
    core = load(tmp_path, "core/pyproject.toml")
    assert core["project"]["dependencies"] == []  # type: ignore[index]
    assert "uv" not in core["tool"]  # type: ignore[operator]  # nothing to derive: no sibling, so no sources table


def test_the_root_pyproject_lists_the_workspace_members(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    assert load(tmp_path, "pyproject.toml")["tool"]["uv"]["workspace"] == {"members": ["core", "shell"]}  # type: ignore[index]


def test_generate_never_rewrites_an_existing_pyproject(tmp_path: Path) -> None:
    repo(tmp_path, extra={"shell/pyproject.toml": '[project]\nname = "mine"\ndependencies = ["requests"]\n'})
    write_generated(policy(), tmp_path, services())
    assert (tmp_path / "shell/pyproject.toml").read_text(encoding="utf-8") == '[project]\nname = "mine"\ndependencies = ["requests"]\n'


def test_a_freshly_generated_tree_has_no_drift(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    assert drift(tmp_path) == []


def test_a_hand_edited_dependency_list_is_reported_naming_the_field_and_the_annotation(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    path = tmp_path / "shell/pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('"beautifulsoup4",', '"requests",'), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    [(subject, detail)] = [(s, d) for s, d in drift(tmp_path) if "dependencies" in s]
    assert subject == "shell/pyproject.toml:project.dependencies"
    assert "beautifulsoup4" in detail and "requests" in detail and "ring" in detail
    write_generated(policy(), tmp_path, services())
    assert path.read_text(encoding="utf-8") == before, "drift is reported, never repaired by overwriting"


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ('core = { workspace = true }', 'core = { git = "https://example.test/x" }', "tool.uv.sources"),
        ('source_roots = ["src"]\n\n[tool.tach]', 'source_roots = ["lib"]\n\n[tool.tach]', "tool.arborescent.source_roots"),
        ('root_packages = ["shell"]', 'root_packages = ["shell", "other"]', "tool.importlinter.root_packages"),
    ],
)
def test_each_derived_table_is_checked(tmp_path: Path, old: str, new: str, field: str) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    path = tmp_path / "shell/pyproject.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    assert [s for s, _ in drift(tmp_path)] == [f"shell/pyproject.toml:{field}"]


def test_a_stale_workspace_member_list_is_reported(tmp_path: Path) -> None:
    repo(tmp_path, extra={"pyproject.toml": '[tool.uv.workspace]\nmembers = ["core"]\n'})
    assert [s for s, _ in drift(tmp_path)] == ["pyproject.toml:tool.uv.workspace.members"]


def test_fields_the_owner_controls_are_never_compared(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    path = tmp_path / "shell/pyproject.toml"
    text = path.read_text(encoding="utf-8").replace('version = "0.0.0"', 'version = "9.9.9"')
    path.write_text(text + '\n[build-system]\nrequires = ["hatchling"]\n', encoding="utf-8")
    assert drift(tmp_path) == []


def test_dependency_version_pins_and_spelling_are_not_derived(tmp_path: Path) -> None:
    repo(tmp_path, extra={"shell/pyproject.toml": '[project]\nname = "shell"\ndependencies = ["Beautifulsoup4>=4.12", "core"]\n'
                          '[tool.uv.sources]\ncore = { workspace = true }\n'})
    assert [s for s, _ in drift(tmp_path) if "dependencies" in s] == []


def test_a_dynamic_dependency_list_is_not_compared(tmp_path: Path) -> None:
    repo(tmp_path, extra={"shell/pyproject.toml": '[project]\nname = "shell"\ndynamic = ["dependencies"]\n'})
    assert [s for s, _ in drift(tmp_path) if "dependencies" in s] == []


CORE_ELSEWHERE = "https://example.test/core.git"


def catalog_with_core_elsewhere() -> str:
    """The same catalog with only core's ``home`` flipped from a workspace member to its own repository."""
    old = '[packages.home]\nkind = "workspace-member"\npath = "core"\n'
    assert old in CATALOG
    return CATALOG.replace(old, f'[packages.home]\nkind = "repository"\nrepository = "{CORE_ELSEWHERE}"\n')


def derived_sources(root: Path, catalog: str) -> dict[str, dict[str, object]]:
    write_files(root, {**LAYOUT, "packages.toml": catalog})
    write_generated(policy(), root, services())
    return {rel: load(root, rel) for rel in ("core/pyproject.toml", "shell/pyproject.toml", "pyproject.toml") if (root / rel).exists()}


def test_changing_a_packages_home_changes_only_its_consumers_sources_and_the_member_list(tmp_path: Path) -> None:
    mono = derived_sources(tmp_path / "mono", CATALOG)
    split = derived_sources(tmp_path / "split", catalog_with_core_elsewhere())
    assert mono["shell/pyproject.toml"]["tool"]["uv"]["sources"] == {"core": {"workspace": True}}  # type: ignore[index]
    assert split["shell/pyproject.toml"]["tool"]["uv"]["sources"] == {"core": {"git": CORE_ELSEWHERE}}  # type: ignore[index]
    assert mono["core/pyproject.toml"] == split["core/pyproject.toml"], "the flipped package's own file does not change"
    del mono["shell/pyproject.toml"]["tool"]["uv"]["sources"]  # type: ignore[attr-defined]
    del split["shell/pyproject.toml"]["tool"]["uv"]["sources"]  # type: ignore[attr-defined]
    assert mono["shell/pyproject.toml"] == split["shell/pyproject.toml"]
    assert mono["pyproject.toml"]["tool"]["uv"]["workspace"]["members"] == ["core", "shell"]  # type: ignore[index]
    assert split["pyproject.toml"]["tool"]["uv"]["workspace"]["members"] == ["shell"]  # type: ignore[index]  # core left the workspace


def test_a_sibling_without_a_home_gets_no_source_entry(tmp_path: Path) -> None:
    repo(tmp_path, CATALOG.replace('[packages.home]\nkind = "workspace-member"\npath = "core"\n', ""))
    write_generated(policy(), tmp_path, services())
    assert "uv" not in load(tmp_path, "shell/pyproject.toml")["tool"]  # type: ignore[operator]
    assert drift(tmp_path) == []


def test_the_member_list_is_the_home_path_not_the_current_root(tmp_path: Path) -> None:
    repo(tmp_path, CATALOG.replace('path = "shell"\n[[packages.paths]]', 'path = "packages/shell"\n[[packages.paths]]'))
    write_generated(policy(), tmp_path, services())
    assert load(tmp_path, "pyproject.toml")["tool"]["uv"]["workspace"]["members"] == ["core", "packages/shell"]  # type: ignore[index]


def test_an_own_repository_source_checks_the_git_url_but_not_the_pin(tmp_path: Path) -> None:
    repo(tmp_path, catalog_with_core_elsewhere(), {
        "shell/pyproject.toml": '[project]\nname = "shell"\ndependencies = ["beautifulsoup4", "core"]\n'
        f'[tool.uv.sources]\ncore = {{ git = "{CORE_ELSEWHERE}", rev = "abc123" }}\n'})
    assert [s for s, _ in drift(tmp_path) if "sources" in s] == []
    write_files(tmp_path, {"shell/pyproject.toml": '[project]\nname = "shell"\ndependencies = ["beautifulsoup4", "core"]\n'
                           '[tool.uv.sources]\ncore = { workspace = true }\n'})
    assert [s for s, _ in drift(tmp_path) if "sources" in s] == ["shell/pyproject.toml:tool.uv.sources"]


def test_the_generated_manifest_satisfies_the_undeclared_imports_practice(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    scan = FsScanner().scan(policy(), tmp_path)
    assert undeclared_imports(FS, tmp_path, policy(), scan.modules, scan.known, "shell/pyproject.toml") == []
    write_files(tmp_path, {"shell/pyproject.toml": '[project]\nname = "shell"\ndependencies = []\n'})
    assert undeclared_imports(FS, tmp_path, policy(), scan.modules, scan.known, "shell/pyproject.toml") != []


def test_two_packages_sharing_a_root_cannot_each_have_a_pyproject(tmp_path: Path) -> None:
    repo(tmp_path, CATALOG.replace('root = "shell"', 'root = "core"'))
    with pytest.raises(CatalogError, match="share the root"):
        run_own(policy(), tmp_path, services())


def test_the_key_is_off_by_default_and_needs_a_catalog() -> None:
    with pytest.raises(PolicyError, match="package_pyproject"):
        mini_policy(package_pyproject=True)
    with pytest.raises(PolicyError, match="package_pyproject"):
        mini_policy(package_catalog="packages.toml", package_pyproject="yes")


def test_no_derivation_runs_without_the_key(tmp_path: Path) -> None:
    repo(tmp_path)
    plain = policy(package_pyproject=False)
    assert write_generated(plain, tmp_path, services()) == [] or not any(p.endswith("pyproject.toml") for p in write_generated(plain, tmp_path, services()))
    assert not (tmp_path / "shell/pyproject.toml").exists()


def test_drift_is_information_that_never_fails_a_check(tmp_path: Path) -> None:
    repo(tmp_path, extra={"pyproject.toml": '[tool.uv.workspace]\nmembers = ["core"]\n'})
    assert Rule.PACKAGE_PYPROJECT_DRIFT in INFORMATIONAL_RULES
    report = run_check(policy(), tmp_path, services(), Options(tools=False, use_baseline=False))
    assert Rule.PACKAGE_PYPROJECT_DRIFT in {note.rule for note in report.notes}
    assert Rule.PACKAGE_PYPROJECT_DRIFT not in {v.rule for v in report.violations}


def mutated_rules(root: Path) -> set[Rule]:
    scan = FsScanner().scan(policy(), root)
    return {m.rule for m in plan(policy(), scan, load_package_context(policy(), root, FS, scan))[0]}


def test_the_drift_rule_has_a_selftest_probe_when_a_derived_file_exists(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    assert Rule.PACKAGE_PYPROJECT_DRIFT in mutated_rules(tmp_path)


def test_the_probe_is_skipped_with_a_reason_when_no_derived_file_exists(tmp_path: Path) -> None:
    repo(tmp_path)
    scan = FsScanner().scan(policy(), tmp_path)
    mutations, skipped = plan(policy(), scan, load_package_context(policy(), tmp_path, FS, scan))
    assert Rule.PACKAGE_PYPROJECT_DRIFT not in {m.rule for m in mutations}
    assert any(Rule.PACKAGE_PYPROJECT_DRIFT in line for line in skipped)


def test_selftest_kills_the_drift_probe(tmp_path: Path) -> None:
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    report = selftest(policy(), tmp_path, services())
    assert report.survived == []
    assert any(entry.startswith(f"[{Rule.PACKAGE_PYPROJECT_DRIFT}]") for entry in report.killed)


def test_selftest_survives_when_the_drift_check_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard against rule removal: with the drift check stubbed out, its probe must report a survivor."""
    repo(tmp_path)
    write_generated(policy(), tmp_path, services())
    monkeypatch.setattr(engine, "package_gap_violations", lambda *args, **kwargs: [])
    report = selftest(policy(), tmp_path, services())
    assert any(entry.startswith(f"[{Rule.PACKAGE_PYPROJECT_DRIFT}]") for entry in report.survived)


def exported(root: Path, **overrides: object) -> dict[str, dict[str, object]]:
    chosen = policy(**overrides)
    document = export_document(migration_arguments_of(chosen, root, FS, FsScanner().scan(chosen, root)), "packages.toml")
    return {item["id"]: item["pyproject"] for item in document["packages"]}


def test_the_export_carries_the_derived_fields_in_its_pyproject_slot(tmp_path: Path) -> None:
    repo(tmp_path)
    assert exported(tmp_path)["shell"] == {
        "path": "shell/pyproject.toml", "requires_python": ">=3.11", "dependencies": ["beautifulsoup4", "core"],
        "sources": {"core": {"workspace": True}}, "workspace_members": [], "source_roots": ["src"],
        "root_packages": ["shell"],
    }


def test_the_export_slot_stays_null_without_the_key(tmp_path: Path) -> None:
    repo(tmp_path)
    assert set(exported(tmp_path, package_pyproject=False).values()) == {None}


def test_the_export_with_the_slot_filled_validates_against_the_published_schema(tmp_path: Path) -> None:
    repo(tmp_path)
    chosen = policy()
    document = export_document(migration_arguments_of(chosen, tmp_path, FS, FsScanner().scan(chosen, tmp_path)), "packages.toml")
    Draft202012Validator(SCHEMA).validate(document)
    assert document["packages"][1]["pyproject"] is not None
    slot = SCHEMA["properties"]["packages"]["items"]["properties"]["pyproject"]
    assert slot["type"] == ["object", "null"], "still object | null: no schema version bump"
    document["packages"][1]["pyproject"]["dependencies"] = "core"
    with pytest.raises(ValidationError):
        Draft202012Validator(SCHEMA).validate(document)


MONOREPO = "https://example.test/mono.git"


def catalog_with_shell_in_its_own_repository(monorepo: str = MONOREPO) -> str:
    """core stays a workspace member of the monorepo it lives in today; shell moves to its own repository."""
    text = CATALOG.replace('[packages.home]\nkind = "workspace-member"\npath = "shell"\n', '[packages.home]\nkind = "repository"\nrepository = "https://example.test/shell.git"\n')
    return text.replace('repository = "example/mono"\nroot = "core"', f'repository = "{monorepo}"\nroot = "core"')


def test_an_own_repository_consumer_of_a_workspace_member_gets_a_git_source_with_a_subdirectory(tmp_path: Path) -> None:
    derived = derived_sources(tmp_path, catalog_with_shell_in_its_own_repository())
    assert derived["shell/pyproject.toml"]["tool"]["uv"]["sources"] == {"core": {"git": MONOREPO, "subdirectory": "core"}}  # type: ignore[index]
    assert "workspace" not in str(derived["shell/pyproject.toml"]["tool"]["uv"]["sources"])  # type: ignore[index]


def test_workspace_true_needs_both_packages_to_be_members_of_the_workspace(tmp_path: Path) -> None:
    derived = derived_sources(tmp_path, CATALOG)
    assert derived["shell/pyproject.toml"]["tool"]["uv"]["sources"] == {"core": {"workspace": True}}  # type: ignore[index]


def test_the_subdirectory_of_a_git_source_is_compared(tmp_path: Path) -> None:
    catalog = catalog_with_shell_in_its_own_repository()
    good = f'[project]\nname = "shell"\ndependencies = ["beautifulsoup4", "core"]\n[tool.uv.sources]\ncore = {{ git = "{MONOREPO}", subdirectory = "core", rev = "abc" }}\n'
    repo(tmp_path, catalog, {"shell/pyproject.toml": good})
    assert [s for s, _ in drift(tmp_path) if "sources" in s] == []
    write_files(tmp_path, {"shell/pyproject.toml": good.replace('subdirectory = "core"', 'subdirectory = "elsewhere"')})
    assert [s for s, _ in drift(tmp_path) if "sources" in s] == ["shell/pyproject.toml:tool.uv.sources"]


def test_a_git_source_built_from_a_value_that_is_not_a_url_is_reported_without_guessing_a_url(tmp_path: Path) -> None:
    repo(tmp_path, catalog_with_shell_in_its_own_repository(monorepo="example/mono"))  # no pyproject.toml exists yet
    notes = [(s, d) for s, d in drift(tmp_path) if "not a git URL" in d]
    assert [s for s, _ in notes] == ["shell/pyproject.toml:tool.uv.sources"]
    assert "example/mono" in notes[0][1]
    write_generated(policy(), tmp_path, services())
    assert 'git = "example/mono"' in (tmp_path / "shell/pyproject.toml").read_text(encoding="utf-8"), "the value is written as given"


def test_urls_are_not_reported(tmp_path: Path) -> None:
    repo(tmp_path, catalog_with_shell_in_its_own_repository())
    assert [d for _, d in drift(tmp_path) if "not a git URL" in d] == []
