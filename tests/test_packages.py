from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from arborescent.cli import main
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.packages import CatalogError, PackageCatalog


def _catalog(root: Path, contents: str) -> PackageCatalog:
    (root / "packages.toml").write_text(contents, encoding="utf-8")
    return PackageCatalog.load(LocalFileSystem(), root)


def test_catalog_loads_stable_package_and_path_metadata(tmp_path: Path) -> None:
    (tmp_path / "src/core").mkdir(parents=True)
    catalog = _catalog(
        tmp_path,
        """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/core"
summary = "Domain-neutral source lifecycle."
capabilities = ["source-runner", "health"]

[[packages.paths]]
path = "src/core"
role = "source"
metadata = { module = "collection_core" }
""",
    )

    descriptor = catalog.get("collection-core")
    assert descriptor.id == "collection-core"
    assert descriptor.paths[0].metadata == {"module": "collection_core"}


def test_extraction_preserves_package_identity_and_path_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src/core"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/core").mkdir(parents=True)
    (tmp_path / "tests/core/test_core.py").write_text(
        "def test_value(): pass\n", encoding="utf-8"
    )
    catalog = _catalog(
        tmp_path,
        """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/core"
summary = "Domain-neutral source lifecycle."

[[packages.paths]]
path = "src/core"
role = "source"
metadata = { import = "collection_core", arborescent_config = "arborescent.toml" }

[[packages.paths]]
path = "tests/core"
role = "tests"
destination = "tests"
""",
    )

    destination = catalog.extract("collection-core", tmp_path / "out/core")

    assert (destination / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (destination / "tests/test_core.py").is_file()
    metadata = tomllib.loads(
        (destination / ".arborescent-package.toml").read_text(encoding="utf-8")
    )
    assert metadata["package"]["id"] == "collection-core"
    assert metadata["paths"][0]["metadata"]["arborescent_config"] == "arborescent.toml"


def test_catalog_rejects_path_overlap_between_packages(tmp_path: Path) -> None:
    (tmp_path / "src/shared").mkdir(parents=True)
    with pytest.raises(CatalogError, match="more than one package"):
        _catalog(
            tmp_path,
            """
[[packages]]
id = "first"
name = "First"
kind = "domain-library"
repository = "org/repo"
root = "src/shared"
summary = "First."
[[packages.paths]]
path = "src/shared"
role = "source"

[[packages]]
id = "second"
name = "Second"
kind = "domain-library"
repository = "org/repo"
root = "src/shared"
summary = "Second."
[[packages.paths]]
path = "src/shared"
role = "source"
""",
        )


def test_extract_rejects_destination_inside_owned_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "src/core"
    source.mkdir(parents=True)
    catalog = _catalog(
        tmp_path,
        """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/core"
summary = "Shared source lifecycle."
[[packages.paths]]
path = "src/core"
role = "source"
""",
    )

    with pytest.raises(CatalogError, match="destination overlaps package source"):
        catalog.extract("collection-core", source / "export")


def test_extract_includes_declared_dependencies_only_when_requested(
    tmp_path: Path,
) -> None:
    (tmp_path / "src/core").mkdir(parents=True)
    (tmp_path / "src/policy").mkdir(parents=True)
    (tmp_path / "src/core/core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src/policy/policy.py").write_text("RULE = True\n", encoding="utf-8")
    catalog = _catalog(
        tmp_path,
        """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/core"
summary = "Shared run lifecycle."
depends_on = ["collection-policy"]
[[packages.paths]]
path = "src/core"
role = "source"

[[packages]]
id = "collection-policy"
name = "Collection Policy"
kind = "domain-library"
repository = "org/repo"
root = "src/policy"
summary = "Domain reconciliation rules."
[[packages.paths]]
path = "src/policy"
role = "source"
""",
    )

    destination = catalog.extract(
        "collection-core", tmp_path / "out/core", include_dependencies=True
    )

    assert (destination / "core.py").is_file()
    assert (destination / "dependencies/collection-policy/policy.py").is_file()
    manifest = tomllib.loads(
        (destination / ".arborescent-package.toml").read_text(encoding="utf-8")
    )
    assert manifest["package"]["included_packages"] == [
        "collection-policy",
        "collection-core",
    ]


def test_catalog_rejects_traversal_and_symlinked_package_paths(tmp_path: Path) -> None:
    (tmp_path / "src/core").mkdir(parents=True)
    with pytest.raises(CatalogError, match="normalized repository-relative path"):
        _catalog(
            tmp_path,
            """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/core"
summary = "Shared run lifecycle."
[[packages.paths]]
path = "src/core"
role = "source"
[[packages.paths]]
path = "../outside"
role = "docs"
""",
        )
    (tmp_path / "src/link").symlink_to(tmp_path / "src/core", target_is_directory=True)
    with pytest.raises(CatalogError, match="cannot be a symlink"):
        _catalog(
            tmp_path,
            """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/link"
summary = "Shared run lifecycle."
[[packages.paths]]
path = "src/link"
role = "source"
""",
        )


def test_cli_extracts_package_from_repository_metadata(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    source = tmp_path / "src/core"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    (tmp_path / "packages.toml").write_text(
        """
[[packages]]
id = "collection-core"
name = "Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "src/core"
summary = "Shared source lifecycle."
[[packages.paths]]
path = "src/core"
role = "source"
""",
        encoding="utf-8",
    )
    destination = tmp_path / "extracted"
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "packages",
                "extract",
                "collection-core",
                "--output",
                str(destination),
            ]
        )
        == 0
    )

    assert (destination / "__init__.py").read_text(encoding="utf-8") == "VALUE = 7\n"
    assert "extracted collection-core" in capsys.readouterr().out
