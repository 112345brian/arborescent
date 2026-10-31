"""The parts of a package's ``pyproject.toml`` that the annotations determine (#60), their template text and their drift. Pure.

Derived: ``[project].dependencies`` (third-party distributions the rings of the package's modules allow, through the policy's
import-name to distribution mapping, plus the sibling packages it declares in ``depends_on``), ``[tool.uv.sources]`` for each
sibling, the workspace ``members`` of the root, and the ``source_roots`` / ``root_packages`` tables of Arborescent, tach and
import-linter. Not derived, and never compared: name, description, version, classifiers, build backend and the pin values
(``rev``, ``tag``, version specifiers). Where a sibling comes from is its ``home`` (#54): a workspace member is
``{ workspace = true }``, a package in its own repository is a git requirement on that repository, and a package
without a ``home`` gets no entry (the migration export reports the missing annotation). The workspace ``members`` are the
``home.path`` of the workspace-member packages. That is the one place ``home`` is read: ``source_of`` and ``derive_projects``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .domain import Rule, Violation, dist_name
from .package_model import CatalogDocument, CatalogError, PackageDescriptor

__all__ = ["DerivedProject", "ProjectFacts", "derive_projects", "drift_violations", "render_template"]

ROOT_FILE = "pyproject.toml"
_NAME = re.compile(r"[<>=!~\[ ;@(]")
_SOURCE_ROOT_TABLES = ("tool.arborescent", "tool.tach")


@dataclass(frozen=True, slots=True)
class ProjectFacts:
    """What the annotations say about the repository, as gathered by the caller."""

    owned: Mapping[str, Sequence[str]]  # package id -> the first-party modules it owns
    module_paths: Mapping[str, str]  # module -> posix path relative to the repository root
    externals: Mapping[str, frozenset[str]]  # module -> import names its ring's ``external`` allow-list names
    distributions: Mapping[str, str]  # import name -> distribution, as the policy maps them
    source_roots: Sequence[str]  # the policy's import roots, repository-relative
    python: str


@dataclass(frozen=True, slots=True)
class DerivedProject:
    """The derived fields of one ``pyproject.toml``. ``None`` / empty means that field is not derived for this file."""

    path: str
    package: PackageDescriptor | None  # None: a workspace root that is not itself a package
    dependencies: tuple[str, ...] | None = None
    sources: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: {})
    members: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()
    root_packages: tuple[str, ...] = ()
    requires_python: str = ""


def pyproject_path(package: PackageDescriptor) -> str:
    return ROOT_FILE if package.root in (".", "") else f"{package.root}/{ROOT_FILE}"


def source_of(consumer: PackageDescriptor, sibling: PackageDescriptor) -> dict[str, object] | None:
    """How ``consumer`` obtains ``sibling``, from their ``home``s; ``None`` when the sibling's ``home`` is not annotated.

    ``{ workspace = true }`` only when both are members of the same workspace. A workspace member that a package outside
    the workspace needs is fetched from the repository it lives in today (its catalog ``repository``) at its ``home.path``;
    a package in its own repository is fetched from that repository. The pin is not derived.
    """
    home = sibling.home
    if home is None:
        return None
    if home.kind == "workspace-member":
        if consumer.home is not None and consumer.home.kind == "workspace-member":
            return {"workspace": True}
        return {"git": sibling.repository, **({} if home.path is None else {"subdirectory": home.path})}
    return None if home.repository is None else {"git": home.repository}


def is_git_url(value: object) -> bool:
    return isinstance(value, str) and ("://" in value or value.startswith("git@"))


def source_notes(project: DerivedProject) -> list[Violation]:
    """A git source built from a value that is not a URL: reported, never replaced by a guessed URL."""
    return [
        Violation(
            Rule.PACKAGE_PYPROJECT_DRIFT, f"{project.path}:tool.uv.sources",
            f"{name} would use git = {entry['git']!r}, which is not a git URL; the repository it comes from (`home.repository`, "
            "or the catalog `repository` of a workspace member) must be one",
        )
        for name, entry in sorted(project.sources.items())
        if "git" in entry and not is_git_url(entry["git"])
    ]


def _check_roots(catalog: CatalogDocument) -> None:
    seen: dict[str, str] = {}
    for package in catalog.packages:
        path = pyproject_path(package)
        if path in seen:
            raise CatalogError(
                f"packages {seen[path]!r} and {package.id!r} share the root {package.root!r}; "
                "each package needs its own root to have its own pyproject.toml"
            )
        seen[path] = package.id


def _relative_root(source_root: str, root: str) -> str | None:
    base = PurePosixPath(source_root)
    if root in (".", ""):
        return base.as_posix()
    try:
        return base.relative_to(root).as_posix()
    except ValueError:
        return None


def _own_source_roots(package: PackageDescriptor, modules: Iterable[str], facts: ProjectFacts) -> tuple[str, ...]:
    found: set[str] = set()
    for module in modules:
        path = PurePosixPath(facts.module_paths.get(module, ""))
        containing = [root for root in facts.source_roots if root in (".", "") or PurePosixPath(root) == path or PurePosixPath(root) in path.parents]
        if containing:
            relative = _relative_root(max(containing, key=len), package.root)
            if relative is not None:
                found.add(relative)
    return tuple(sorted(found))


def derive_projects(catalog: CatalogDocument, facts: ProjectFacts) -> tuple[DerivedProject, ...]:
    """One derived project per package, plus the workspace root when it lists members and is no package's file."""
    _check_roots(catalog)
    by_id = {package.id: package for package in catalog.packages}
    members = tuple(sorted({
        path for package in catalog.packages
        if package.home is not None and package.home.kind == "workspace-member" and (path := package.home.path) is not None
    }))
    out: list[DerivedProject] = []
    for package in catalog.packages:
        modules = sorted(facts.owned.get(package.id, ()))
        allowed = {name for module in modules for name in facts.externals.get(module, ())}
        third_party = {facts.distributions[name] for name in allowed if name in facts.distributions}
        names = sorted({*third_party, *package.depends_on}, key=dist_name)
        sources = {dep: entry for dep in package.depends_on if dep in by_id and (entry := source_of(package, by_id[dep])) is not None}
        roots = _own_source_roots(package, modules, facts)
        out.append(DerivedProject(
            pyproject_path(package), package, tuple(names), sources,
            members if pyproject_path(package) == ROOT_FILE else (),
            roots, tuple(sorted({module.split(".")[0] for module in modules})), f">={facts.python}",
        ))
    if members and not any(project.path == ROOT_FILE for project in out):
        out.append(DerivedProject(ROOT_FILE, None, members=members, requires_python=f">={facts.python}"))
    return tuple(out)


def export_of(project: DerivedProject) -> dict[str, object]:
    """The derived fields as the ``pyproject`` object of the migration export (#54); keys and order are stable."""
    return {
        "path": project.path,
        "requires_python": project.requires_python,
        "dependencies": None if project.dependencies is None else list(project.dependencies),
        "sources": {name: dict(entry) for name, entry in sorted(project.sources.items())},
        "workspace_members": list(project.members),
        "source_roots": list(project.source_roots),
        "root_packages": list(project.root_packages),
    }


def _q(value: str) -> str:
    return json.dumps(value)


def _array(items: Sequence[str]) -> str:
    return "[" + ", ".join(_q(item) for item in items) + "]"


def _inline(entry: Mapping[str, object]) -> str:
    return "{ " + ", ".join(f"{key} = {'true' if value is True else _q(str(value))}" for key, value in sorted(entry.items())) + " }"


def render_template(project: DerivedProject) -> str:
    """A new file: the derived fields plus a minimal template the owner edits. No build backend is invented."""
    lines = [
        "# Created once by `arborescent generate` from the package annotations. Only the derived fields (dependencies,",
        "# uv sources and workspace members, source roots, root packages) are checked, as `package-pyproject-drift`;",
        "# everything else is yours, and this file is never overwritten.",
    ]
    package = project.package
    if package is not None:
        lines += [
            "[project]", f"name = {_q(package.id)}", 'version = "0.0.0"', f"description = {_q(package.summary)}",
            f"requires-python = {_q(project.requires_python)}",
        ]
        deps = project.dependencies or ()
        lines += ["dependencies = [" + ("" if not deps else "\n" + "".join(f"    {_q(dep)},\n" for dep in deps)) + "]", ""]
    if project.sources:
        lines += ["[tool.uv.sources]"]
        lines += [f"{name} = {_inline(entry)}" for name, entry in sorted(project.sources.items())]
        lines += [""]
    if project.members:
        lines += ["[tool.uv.workspace]", f"members = {_array(project.members)}", ""]
    if project.source_roots:
        for table in _SOURCE_ROOT_TABLES:
            lines += [f"[{table}]", f"source_roots = {_array(project.source_roots)}", ""]
    if project.root_packages:
        lines += ["[tool.importlinter]", f"root_packages = {_array(project.root_packages)}", ""]
    return "\n".join(lines).rstrip() + "\n"


def _table(data: Mapping[str, Any], dotted: str) -> Mapping[str, Any]:
    current: Any = data
    for key in dotted.split("."):
        current = current.get(key) if isinstance(current, dict) else None
    return current if isinstance(current, dict) else {}


def _requirement_names(items: object) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {dist_name(_NAME.split(str(item).strip(), maxsplit=1)[0]) for item in items}  # pyright: ignore[reportUnknownVariableType]


def _strings(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {PurePosixPath(str(item)).as_posix() for item in value}  # pyright: ignore[reportUnknownVariableType]


def _drift(path: str, name: str, actual: object, expected: object, why: str) -> Violation:
    return Violation(Rule.PACKAGE_PYPROJECT_DRIFT, f"{path}:{name}", f"is {actual}, the annotations derive {expected} ({why})")


def drift_violations(project: DerivedProject, data: Mapping[str, Any]) -> list[Violation]:
    """Fields of the existing file ``data`` that disagree with the annotations. The file is never changed."""
    out: list[Violation] = []
    package = project.package
    if package is not None and project.dependencies is not None and "dependencies" not in _table(data, "project").get("dynamic", ()):
        actual = _table(data, "project").get("dependencies")
        if _requirement_names(actual) != {dist_name(d) for d in project.dependencies}:
            out.append(_drift(
                project.path, "project.dependencies", sorted(_requirement_names(actual)), list(project.dependencies),
                f"the `external` allow-lists of the rings of {package.id!r}'s modules and its `depends_on`",
            ))
    if package is not None:
        out += _source_drift(project, package, _table(data, "tool.uv.sources"))
    members = _table(data, "tool.uv.workspace").get("members")
    if project.members and _strings(members) != set(project.members):
        out.append(_drift(project.path, "tool.uv.workspace.members", sorted(_strings(members)), list(project.members),
                          "the `home.path` of the workspace-member packages"))
    for table in _SOURCE_ROOT_TABLES:
        out += _present_list(project, f"{table}.source_roots", data, project.source_roots, "the policy's source roots under the package root")
    out += _present_list(project, "tool.importlinter.root_packages", data, project.root_packages, "the top-level packages of the package's modules")
    return out


def _present_list(project: DerivedProject, name: str, data: Mapping[str, Any], expected: Sequence[str], why: str) -> list[Violation]:
    table, _, key = name.rpartition(".")
    actual = _table(data, table).get(key)
    if expected and actual is not None and _strings(actual) != set(expected):
        return [_drift(project.path, name, sorted(_strings(actual)), list(expected), why)]
    return []


def _source_drift(project: DerivedProject, package: PackageDescriptor, actual: Mapping[str, Any]) -> list[Violation]:
    out: list[Violation] = []
    present = {dist_name(key): (key, value) for key, value in actual.items()}
    for name, entry in sorted(project.sources.items()):
        key, value = present.get(dist_name(name), (name, None))
        value = value if isinstance(value, dict) else {}
        agrees = (
            value == {"workspace": True} if entry.get("workspace") is True
            else "workspace" not in value and value.get("git", entry["git"]) == entry["git"] and bool(value)
            and value.get("subdirectory", entry.get("subdirectory")) == entry.get("subdirectory")
        )
        if not agrees:
            out.append(_drift(project.path, "tool.uv.sources", f"{key} = {value or 'missing'}", f"{name} = {dict(entry)}",
                              f"{package.id!r} `depends_on` {name!r}, and {name!r}'s `home` decides workspace or own repository"))
    for key, value in sorted(actual.items()):
        if dist_name(key) not in {dist_name(n) for n in project.sources} and isinstance(value, dict) and value.get("workspace") is True:  # pyright: ignore[reportUnknownMemberType]
            out.append(_drift(project.path, "tool.uv.sources", f"{key} = a workspace source", "none", f"{package.id!r} does not list {key!r} in `depends_on`"))
    return out
