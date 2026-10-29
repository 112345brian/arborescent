"""Extraction keeps package semantics stable and records origin, current location and operation state apart."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arborescent_helpers import write_files

from arborescent.cli import main
from arborescent.config import load_policy
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

    destination = catalog.extract(
        "collection-core", tmp_path / "out/core", repository="org/vendored"
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


def test_repository_defaults_to_origin_and_must_not_be_blank(tmp_path: Path) -> None:
    _, catalog = _repo(tmp_path)

    extracted = load_extracted_package(
        FS, catalog.extract("collection-core", tmp_path / "out/core")
    )
    assert extracted.current_repository == extracted.origin_repository == "org/repo"
    with pytest.raises(CatalogError, match="repository must be a non-empty string"):
        catalog.extract("collection-core", tmp_path / "out/blank", repository="  ")


def test_round_trip_into_differently_named_directories_keeps_ring_assignments(
    tmp_path: Path,
) -> None:
    root, catalog = _repo(tmp_path)
    first = catalog.extract("collection-core", tmp_path / "out/first-name")
    second = catalog.extract("collection-core", tmp_path / "elsewhere/another")

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
    assert expected[1] == []  # the extracted policy governs itself with no violations


def test_round_trip_with_dependencies_keeps_each_nested_policy(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path)

    destination = catalog.extract(
        "collection-core", tmp_path / "out/core", include_dependencies=True
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


def test_layout_change_that_would_change_module_identity_is_an_explicit_error(
    tmp_path: Path,
) -> None:
    root, _ = _repo(tmp_path)
    write_files(
        root,
        {
            "packages.toml": CATALOG.replace(
                'path = "packages/core/src"\nrole = "source"',
                'path = "packages/core/src"\nrole = "source"\ndestination = "lib"',
            )
        },
    )
    catalog = PackageCatalog.load(FS, root)

    with pytest.raises(CatalogError, match="would change module identity"):
        catalog.extract("collection-core", tmp_path / "out/core")
    assert not (tmp_path / "out/core").exists()


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
        PackageCatalog.load(FS, root).extract("collection-core", tmp_path / "out/core")


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
    destination = catalog.extract("collection-core", tmp_path / "out/core")
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
    destination = catalog.extract(
        "collection-core", tmp_path / "out/core", include_dependencies=True
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
    destination = catalog.extract("collection-core", tmp_path / "out/core")

    assert main(["packages", "validate", "--extracted", str(destination)]) == 0
    assert "valid: extracted collection-core" in capsys.readouterr().out
    (destination / "src").rename(destination / "moved")
    assert main(["packages", "validate", "--extracted", str(destination)]) == 2
