"""Extraction keeps package semantics stable and records origin, current location and operation state apart."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from arborescent_helpers import write_files
from extracted_tree import extract_tree

from arborescent.certification import _native_minimum
from arborescent.cli import main
from arborescent.config import load_policy
from arborescent.domain import Rule, Violation
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.packages import (
    MANIFEST_NAME,
    CatalogError,
    PackageCatalog,
    load_extracted_package,
    semantics_of,
)
from arborescent.rules import check_modules
from arborescent.scanner import FsScanner

FS = LocalFileSystem()

CHILD_POLICY = """
exclude = ["tests"]
sources = ["src"]
source_roots = ["src"]
rings = ["domain", "adapters"]
[ring.domain]
may_import = []
external = []
[ring.adapters]
may_import = ["domain"]
external = []
[classify]
"core" = "domain"
"core.*" = "domain"
"policy" = "domain"
"policy.*" = "domain"
"""

CATALOG = """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
domain = "collections"
repository = "org/repo"
root = "packages/core"
summary = "Domain-neutral source lifecycle."
capabilities = ["source-runner", "health"]
depends_on = ["collection-policy"]
extraction_status = "experimental"

[[packages.paths]]
path = "packages/core/src"
role = "source"
metadata = { import = "core", boundary = "public", arborescent_config = "arborescent.toml" }

[[packages.paths]]
path = "packages/core/arborescent.toml"
role = "config"

[[packages.paths]]
path = "packages/core/tests"
role = "tests"

[[packages]]
id = "collection-policy"
name = "Collection Policy"
kind = "domain-library"
repository = "org/repo"
root = "packages/policy"
summary = "Reconciliation rules."

[[packages.paths]]
path = "packages/policy/src"
role = "source"
metadata = { import = "policy", arborescent_config = "arborescent.toml" }

[[packages.paths]]
path = "packages/policy/arborescent.toml"
role = "config"
"""

FILES = {
    "packages.toml": CATALOG,
    "packages/core/arborescent.toml": CHILD_POLICY,
    "packages/core/src/core/__init__.py": "VALUE = 1\n",
    "packages/core/src/core/api.py": '__arbor__ = {"ring": "adapters"}\nfrom core import VALUE\n',
    "packages/core/tests/test_core.py": "def test_value(): pass\n",
    "packages/policy/arborescent.toml": CHILD_POLICY,
    "packages/policy/src/policy/__init__.py": "RULE = True\n",
}


def _repo(tmp_path: Path) -> tuple[Path, PackageCatalog]:
    root = tmp_path / "repo"
    write_files(root, FILES)
    return root, PackageCatalog.load(FS, root)


def _rings(directory: Path) -> tuple[dict[str, str | None], list[object]]:
    policy, root = load_policy(FS, directory / "arborescent.toml")
    scan = FsScanner().scan(policy, root)
    rings = {
        name: policy.resolve_ring(scan.arbor_index, name)
        for name in sorted(scan.modules)
    }
    return rings, check_modules(
        policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index
    )  # type: ignore[return-value]


def test_manifest_separates_semantics_origin_location_and_operation_state(
    tmp_path: Path,
) -> None:
    root, catalog = _repo(tmp_path)

    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core", repository="org/vendored"
    )
    extracted = load_extracted_package(FS, destination)

    assert extracted.semantics == semantics_of(catalog.get("collection-core"))
    assert extracted.semantics.capabilities == ("source-runner", "health")
    assert extracted.semantics.depends_on == ("collection-policy",)
    assert extracted.semantics.domain == "collections"
    assert (
        extracted.origin_repository,
        extracted.origin_root,
        extracted.origin_status,
    ) == (
        "org/repo",
        "packages/core",
        "experimental",
    )
    assert (extracted.current_repository, extracted.current_root, extracted.status) == (
        "org/vendored",
        ".",
        "extracted",
    )
    by_role = {item.role: item for item in extracted.paths}
    assert by_role["source"].origin_path == "packages/core/src"
    assert by_role["source"].current_path == "src"
    assert by_role["source"].metadata == {
        "import": "core",
        "boundary": "public",
        "arborescent_config": "arborescent.toml",
    }
    assert by_role["tests"].origin_path == "packages/core/tests"
    assert by_role["tests"].current_path == "tests"
    assert (
        root / by_role["source"].origin_path
    ).is_dir()  # origin stays traceable, and distinct from current


def test_repository_defaults_to_origin(tmp_path: Path) -> None:
    _, catalog = _repo(tmp_path)

    extracted = load_extracted_package(
        FS, extract_tree(catalog, "collection-core", tmp_path / "out/core")
    )
    assert extracted.current_repository == extracted.origin_repository == "org/repo"


def test_round_trip_into_differently_named_directories_keeps_ring_assignments(
    tmp_path: Path,
) -> None:
    root, catalog = _repo(tmp_path)
    first = extract_tree(catalog, "collection-core", tmp_path / "out/first-name")
    second = extract_tree(catalog, "collection-core", tmp_path / "elsewhere/another")

    assert load_extracted_package(FS, first) == load_extracted_package(FS, second)
    for relative in ("arborescent.toml", "src/core/api.py"):
        original = (root / "packages/core" / relative).read_bytes()
        assert (
            (first / relative).read_bytes()
            == (second / relative).read_bytes()
            == original
        )
    expected = _rings(root / "packages/core")
    assert expected[0] == {
        "core": "domain",
        "core.api": "adapters",
    }  # the in-file annotation, not just classify
    assert _rings(first) == _rings(second) == expected
    # the annotation overrides `classify` for enforcement, but the disagreement is still surfaced
    assert expected[1] == [
        Violation(Rule.ARBOR_RING_MISMATCH, "core.api", "__arbor__ names ring 'adapters', but `classify` would assign 'domain'"),
    ]


def test_round_trip_with_dependencies_keeps_each_nested_policy(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path)

    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core", include_dependencies=True
    )
    extracted = load_extracted_package(FS, destination)

    assert extracted.included_packages == ("collection-policy", "collection-core")
    [dependency] = extracted.dependencies
    assert dependency.semantics == semantics_of(catalog.get("collection-policy"))
    assert (dependency.origin_repository, dependency.origin_root) == (
        "org/repo",
        "packages/policy",
    )
    assert dependency.current_root == "dependencies/collection-policy"
    nested = destination / dependency.current_root
    assert (nested / "arborescent.toml").read_bytes() == (
        root / "packages/policy/arborescent.toml"
    ).read_bytes()
    assert _rings(nested) == _rings(root / "packages/policy")
    dependency_paths = [
        item for item in extracted.paths if item.package_id == "collection-policy"
    ]
    assert {item.current_path for item in dependency_paths} == {
        "dependencies/collection-policy/src",
        "dependencies/collection-policy/arborescent.toml",
    }


def test_declared_nested_config_that_is_not_shipped_is_an_error(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path)
    write_files(
        root,
        {
            "packages.toml": CATALOG.replace(
                '[[packages.paths]]\npath = "packages/core/arborescent.toml"\nrole = "config"\n\n',
                "",
                1,
            )
        },
    )

    with pytest.raises(CatalogError, match="missing from the extraction"):
        extract_tree(PackageCatalog.load(FS, root), "collection-core", tmp_path / "out/core")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("manifest_version = 2", "manifest_version = 1", "manifest_version"),
        (
            'current_path = "src"',
            'current_path = "../src"',
            "normalized repository-relative path",
        ),
        (
            'current_path = "src"',
            'current_path = "gone"',
            "does not resolve inside the extracted tree",
        ),
        (
            'package_id = "collection-core"\nrole = "tests"',
            'package_id = "other"\nrole = "tests"',
            "not included",
        ),
    ],
)
def test_tampered_manifests_are_rejected(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    _, catalog = _repo(tmp_path)
    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core")
    manifest = destination / MANIFEST_NAME
    text = manifest.read_text(encoding="utf-8")
    assert old in text
    manifest.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(CatalogError, match=message):
        load_extracted_package(FS, destination)


def test_manifest_is_valid_toml_with_stable_and_location_tables_apart(
    tmp_path: Path,
) -> None:
    _, catalog = _repo(tmp_path)
    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core", include_dependencies=True
    )

    data = tomllib.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))

    assert set(data["package"]) == {
        "id",
        "name",
        "kind",
        "domain",
        "summary",
        "capabilities",
        "depends_on",
    }
    assert set(data["origin"]) == {"repository", "root", "extraction_status"}
    assert set(data["location"]) == {"repository", "root"}
    assert set(data["extraction"]) == {"status", "included_packages"}


def test_cli_validates_an_extracted_package_and_reports_a_broken_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, catalog = _repo(tmp_path)
    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core")

    assert main(["packages", "validate", "--extracted", str(destination)]) == 0
    assert "valid: extracted collection-core" in capsys.readouterr().out
    (destination / "src").rename(destination / "moved")
    assert main(["packages", "validate", "--extracted", str(destination)]) == 2


def test_non_bmp_and_special_characters_in_metadata_survive_extraction(
    tmp_path: Path,
) -> None:
    summary = 'rocket \U0001f680, "quotes", back\\slash and\ttab'
    catalog = CATALOG.replace(
        'summary = "Domain-neutral source lifecycle."',
        f"summary = {_toml_literal(summary)}",
    )
    root, _ = _repo(tmp_path)
    write_files(root, {"packages.toml": catalog})

    destination = extract_tree(PackageCatalog.load(FS, root), "collection-core", tmp_path / "out/core"
    )

    assert load_extracted_package(FS, destination).semantics.summary == summary


def _toml_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    return f'"{escaped}"'


def test_a_shipped_policy_is_validated_even_without_arborescent_config_metadata(
    tmp_path: Path,
) -> None:
    root, _ = _repo(tmp_path)
    write_files(root, {"packages/policy/arborescent.toml": "this = is [not valid"})
    catalog = PackageCatalog.load(FS, root)
    catalog = PackageCatalog(
        FS,
        root,
        [package for package in catalog.packages if package.id == "collection-policy"],
    )

    with pytest.raises(CatalogError, match="shipped policy"):
        extract_tree(catalog, "collection-policy", tmp_path / "out/policy")


def test_revalidation_rejects_a_symlink_that_appears_in_the_tree_but_not_in_generated_directories(
    tmp_path: Path,
) -> None:
    _, catalog = _repo(tmp_path)
    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core")
    venv = destination / ".venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(tmp_path / "missing-interpreter")
    load_extracted_package(
        FS, destination
    )  # a virtualenv created later is ordinary development

    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    (destination / "leak").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(CatalogError, match="contains a symlink"):
        load_extracted_package(FS, destination)


@pytest.mark.parametrize("mode", ["certify", "parent"])
def test_parent_policy_neither_governs_nor_is_changed_by_a_childs_extraction(
    tmp_path: Path, mode: str
) -> None:
    root, catalog = _repo(tmp_path)
    parent = root / "arborescent.toml"
    parent.write_text(
        f'children = ["packages/core"]\nchild_policy_mode = "{mode}"\n'
        + CHILD_POLICY.replace('sources = ["src"]\nsource_roots = ["src"]\n', ""),
        encoding="utf-8",
    )
    before = parent.read_bytes()

    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core")

    assert parent.read_bytes() == before
    assert (destination / "arborescent.toml").read_bytes() == (
        root / "packages/core/arborescent.toml"
    ).read_bytes()
    assert load_policy(FS, parent)[0].child_policy_mode == mode
    assert not (destination / "children").exists()
    assert "child_policy_mode" not in (destination / MANIFEST_NAME).read_text(
        encoding="utf-8"
    )
    assert _rings(destination) == _rings(root / "packages/core")


def test_extracted_policy_meets_the_native_minimum_profile_on_its_own(
    tmp_path: Path,
) -> None:
    """Static profile requirements only: Ruff and Tach version certification needs the real tools."""
    _, catalog = _repo(tmp_path)
    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core")
    policy, root = load_policy(FS, destination / "arborescent.toml")

    assert _native_minimum(policy, FsScanner().scan(policy, root)) == []


def _simple_catalog(extra_paths: str = "", root: str = "packages/core") -> str:
    return (
        '[[packages]]\nid = "collection-core"\nname = "Core"\nkind = "lib"\nrepository = "org/repo"\n'
        f'root = "{root}"\nsummary = "s"\n' + extra_paths
    )


PYPROJECT_POLICY = (
    '[tool.arborescent]\nsources = ["src"]\nsource_roots = ["src"]\nrings = ["domain"]\n'
    '[tool.arborescent.ring.domain]\nmay_import = []\nexternal = []\n[tool.arborescent.classify]\n"*" = "domain"\n'
)


def test_a_pyproject_policy_is_checked_like_an_arborescent_toml(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    files = {"p/src/a.py": "", "p/pyproject.toml": PYPROJECT_POLICY}
    paths = '[[packages.paths]]\npath = "p/src"\nrole = "source"\n{dest}\n[[packages.paths]]\npath = "p/pyproject.toml"\nrole = "config"\n'
    write_files(
        root,
        {
            **files,
            "packages.toml": _simple_catalog(
                paths.format(dest='destination = "lib"'), "p"
            ),
        },
    )
    with pytest.raises(CatalogError, match="expects 'src'"):
        extract_tree(PackageCatalog.load(FS, root), "collection-core", tmp_path / "out/moved")

    write_files(root, {"packages.toml": _simple_catalog(paths.format(dest=""), "p")})
    assert (
        extract_tree(PackageCatalog.load(FS, root), "collection-core", tmp_path / "out/ok")
        / "src/a.py"
    ).is_file()


def test_a_pyproject_without_an_arborescent_table_is_not_treated_as_a_policy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    write_files(
        root,
        {
            "p/a.py": "",
            "p/pyproject.toml": "[project]\nname = 'x'\n",
            "packages.toml": _simple_catalog(
                '[[packages.paths]]\npath = "p"\nrole = "source"\n', "p"
            ),
        },
    )

    assert (
        extract_tree(PackageCatalog.load(FS, root), "collection-core", tmp_path / "out")
        / "a.py"
    ).is_file()


class _RacyFileSystem(LocalFileSystem):
    """Reports the destination as absent once, then it appears holding someone else's file."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.lied = False

    def exists(self, path: Path) -> bool:
        if path == self.destination and not self.lied:
            self.lied = True
            self.destination.mkdir(parents=True)
            (self.destination / "precious.txt").write_text("keep", encoding="utf-8")
            return False
        return super().exists(path)


def _tampered(
    tmp_path: Path, edit: Callable[[str], str], *, dependencies: bool = False
) -> Path:
    _, catalog = _repo(tmp_path)
    destination = extract_tree(catalog, "collection-core", tmp_path / "out/core", include_dependencies=dependencies
    )
    manifest = destination / MANIFEST_NAME
    text = manifest.read_text(encoding="utf-8")
    edited = edit(text)
    assert edited != text
    manifest.write_text(edited, encoding="utf-8")
    return destination


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda t: t.replace('"collection-core"', '"Bad ID/../x"'),
            "stable lowercase slug",
        ),
        (
            lambda t: t.replace('status = "extracted"', 'status = "banana"'),
            "must be 'extracted'",
        ),
        (
            lambda t: t.replace(
                'origin_path = "packages/core/tests"', 'origin_path = "/etc/passwd"'
            ),
            "normalized",
        ),
        (
            lambda t: t.replace(
                'origin_path = "packages/core/tests"', 'origin_path = "../../.."'
            ),
            "normalized",
        ),
        (lambda t: t[: t.index("[[paths]]")], "declares no paths"),
    ],
)
def test_hand_edited_manifests_that_break_invariants_are_rejected(
    tmp_path: Path,
    edit: Callable[[str], str],
    message: str,
) -> None:
    destination = _tampered(tmp_path, edit)

    with pytest.raises(CatalogError, match=message):
        load_extracted_package(FS, destination)


def test_a_dependency_path_outside_its_current_root_is_rejected(tmp_path: Path) -> None:
    destination = _tampered(
        tmp_path,
        lambda t: t.replace(
            'current_path = "dependencies/collection-policy/src"',
            'current_path = "src"',
        ),
        dependencies=True,
    )

    with pytest.raises(
        CatalogError, match="outside package collection-policy's current root"
    ):
        load_extracted_package(FS, destination)


def test_a_duplicated_dependency_entry_is_rejected(tmp_path: Path) -> None:
    def duplicate(text: str) -> str:
        start = text.index("[[dependencies]]")
        end = text.index("[[paths]]")
        return text[:end] + text[start:end] + text[end:]

    with pytest.raises(CatalogError, match="does not match"):
        load_extracted_package(FS, _tampered(tmp_path, duplicate, dependencies=True))


def test_validating_a_missing_or_unreadable_directory_is_a_catalog_error_not_a_crash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist"

    with pytest.raises(CatalogError, match="cannot read extracted package"):
        load_extracted_package(FS, missing)
    assert main(["packages", "validate", "--extracted", str(missing)]) == 2
    assert "cannot read extracted package" in capsys.readouterr().err
