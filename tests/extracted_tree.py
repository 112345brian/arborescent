"""Build an extracted tree for a test: the owned files copied beside a ``.arborescent-package.toml``.

Production no longer copies anything (``packages extract`` hands the work to a migration library such as git filter-repo and
writes the manifest). The manifest is still Arborescent's own data and is read by ``packages validate --extracted`` and by the
preservation checks, so tests that exercise those need a tree to read. This is that tree, nothing more: no symlink or
generated-output handling, which belonged to the copier that was removed.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from arborescent.adapters import LocalFileSystem
from arborescent.package_fields import extraction_target
from arborescent.package_manifest import load_extracted_package
from arborescent.package_model import MANIFEST_NAME, PackageDescriptor, Placement
from arborescent.package_render import render_manifest
from arborescent.packages import PackageCatalog

FS = LocalFileSystem()


def _closure(catalog: PackageCatalog, package: PackageDescriptor) -> tuple[PackageDescriptor, ...]:
    ordered: list[PackageDescriptor] = []
    seen: set[str] = set()

    def visit(current: PackageDescriptor) -> None:
        if current.id in seen:
            return
        seen.add(current.id)
        for dependency in current.depends_on:
            visit(catalog.get(dependency))
        ordered.append(current)

    visit(package)
    return tuple(ordered)


def extract_tree(
    catalog: PackageCatalog, package_id: str, destination: Path, *, include_dependencies: bool = False,
    repository: str | None = None,
) -> Path:
    package = catalog.get(package_id)
    included = _closure(catalog, package) if include_dependencies else (package,)
    destination.mkdir(parents=True)
    placements: list[Placement] = []
    for descriptor in included:
        for item in descriptor.paths:
            relative = extraction_target(descriptor, item)
            if descriptor.id != package.id:
                relative = PurePosixPath("dependencies", descriptor.id) / relative
            source = catalog.root.joinpath(*PurePosixPath(item.path).parts)
            target = destination.joinpath(*relative.parts) if relative.parts else destination
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            placements.append(Placement(descriptor.id, item, relative.as_posix() if relative.parts else "."))
    text = render_manifest(package, included, placements, (repository or package.repository).strip())
    (destination / MANIFEST_NAME).write_text(text, encoding="utf-8")
    load_extracted_package(FS, destination)
    return destination
