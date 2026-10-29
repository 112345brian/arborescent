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


_ARTIFACT_PACKAGE = """
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
"""


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_extraction_omits_generated_artifacts_and_keeps_declared_content(
    tmp_path: Path,
) -> None:
    kept = {
        "__init__.py": "VALUE = 1\n",
        "pkg/module.py": "VALUE = 2\n",
        "tests/test_core.py": "def test_value(): pass\n",
        "docs/guide.md": "# Guide\n",
        "arborescent.toml": "preset = 'strict'\n",
        "pyproject.toml": "[project]\nname = 'core'\n",
        "pkg/data/dist-info.txt": "not a dist directory\n",
        "pkg/build.py": "VALUE = 3\n",
    }
    generated = {
        "__pycache__/mod.cpython-312.pyc": "x",
        "pkg/__pycache__/module.cpython-312.pyc": "x",
        "pkg/stray.pyc": "x",
        ".venv/lib/site.py": "x",
        ".ruff_cache/CACHEDIR.TAG": "x",
        ".mypy_cache/meta.json": "x",
        ".tox/py/log.txt": "x",
        "build/lib/pkg.py": "x",
        "dist/pkg-1.0.tar.gz": "x",
        "pkg/build/inner.py": "x",
    }
    _write(tmp_path / "src/core", {**kept, **generated})
    catalog = _catalog(tmp_path, _ARTIFACT_PACKAGE)

    destination = catalog.extract("collection-core", tmp_path / "out/core")

    extracted = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert extracted == {*kept, ".arborescent-package.toml"}
    assert tomllib.loads(
        (destination / ".arborescent-package.toml").read_text(encoding="utf-8")
    )["package"]["id"] == "collection-core"


def test_extraction_skips_symlinks_inside_generated_directories(
    tmp_path: Path,
) -> None:
    """A virtualenv is full of symlinks; skipping it must not trip the symlink guard."""
    _write(tmp_path / "src/core", {"__init__.py": "VALUE = 1\n"})
    venv_bin = tmp_path / "src/core/.venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(Path("/usr/bin/env"))
    catalog = _catalog(tmp_path, _ARTIFACT_PACKAGE)

    destination = catalog.extract("collection-core", tmp_path / "out/core")

    assert (destination / "__init__.py").is_file()
    assert not (destination / ".venv").exists()


def test_extraction_still_rejects_symlinks_outside_generated_directories(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "src/core", {"__init__.py": "VALUE = 1\n"})
    (tmp_path / "src/core/escape").symlink_to(Path("/usr/bin/env"))
    catalog = _catalog(tmp_path, _ARTIFACT_PACKAGE)

    with pytest.raises(CatalogError, match="does not copy symlinks"):
        catalog.extract("collection-core", tmp_path / "out/core")


def test_extraction_copies_an_explicitly_declared_path_named_build(
    tmp_path: Path,
) -> None:
    """Only nested artifacts are filtered: a declared path (or a package called `build`) is source."""
    _write(tmp_path / "src/build", {"__init__.py": "VALUE = 1\n", "dist/old.txt": "x"})
    catalog = _catalog(
        tmp_path,
        _ARTIFACT_PACKAGE.replace('root = "src/core"', 'root = "src/build"').replace(
            'path = "src/core"', 'path = "src/build"'
        ),
    )

    destination = catalog.extract("collection-core", tmp_path / "out/core")

    assert (destination / "__init__.py").is_file()
    assert not (destination / "dist").exists()


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
