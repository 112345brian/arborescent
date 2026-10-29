"""Repository-hygiene practices: small rules seeded with problems that really happened.

Each practice has a reproduction test in ``tests/test_practices.py`` that builds the broken repository in a temp
directory. Enable them under ``[tool.arborescent.practices]``.

``gitignore-shadow``    an unanchored ignore pattern hid a package directory (empty wheel, files never committed)
``sibling-versions``    a monorepo meta-package pinned ``==0.1.1`` while the component was ``0.2.0``
``secrets-guard``       tests can read the developer's real secrets unless conftest scrubs the environment
``docs-sync``           the architecture doc's module table drifted from the policy
``crlf``                a shell script with CRLF line endings does not run
``undeclared-imports``  code imports a third-party package that no manifest declares
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .domain import NOISE_DIRS, Policy, Rule, Violation, dist_name
from .facts import ModuleInfo, resolve_targets
from .ports import FileSystem, ScanResult, ToolRunner

__all__ = ["PRACTICES", "run_practices"]

_NOISE = NOISE_DIRS | {"build", "dist"}  # build output is noise for gitignore-shadow, but a package called `build` is source


def gitignore_shadow(root: Path, runner: ToolRunner, allow: Sequence[str] = ()) -> list[Violation]:
    listed = runner.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard"], root)
    if listed.returncode != 0:
        return []  # not a git repository (or git missing): nothing to compare against
    out: list[Violation] = []
    seen: set[str] = set()
    for rel in listed.stdout.splitlines():
        parts = rel.split("/")
        if not rel.endswith(".py") or _NOISE & set(parts) or any(fnmatch.fnmatch(parts[-1], a) for a in allow):
            continue
        folder = "/".join(parts[:-1]) or "."
        if folder in seen:
            continue
        seen.add(folder)
        why = runner.run(["git", "check-ignore", "-v", "--", rel], root).stdout.strip()
        pattern = why.split("\t")[0] if why else "?"
        out.append(Violation(Rule.GITIGNORE_SHADOW, folder, f"python source is ignored by {pattern} (never committed, missing from wheels)"))
    return out


def _version_tuple(text: str) -> tuple[int, ...]:
    m = re.match(r"\d+(?:\.\d+)*", text.strip())
    return tuple(int(p) for p in m.group(0).split(".")) if m else (0,)


def _satisfies(version: str, spec: str) -> bool:
    have = _version_tuple(version)
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        m = re.match(r"(==|!=|~=|>=|<=|>|<)\s*([\w.*+-]+)", clause)
        if not m:
            continue
        op, want_text = m.groups()
        want = _version_tuple(want_text)
        n = max(len(have), len(want))
        a, b = have + (0,) * (n - len(have)), want + (0,) * (n - len(want))
        if want_text.endswith(".*"):
            ok = have[: len(want)] == want
            if (op == "==") != ok:
                return False
            continue
        ok = {
            "==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b,
            "~=": a >= b and have[: max(len(want) - 1, 1)] == want[: max(len(want) - 1, 1)],
        }[op]  # fmt: skip
        if not ok:
            return False
    return True


def _dependencies(project: Mapping[str, Any], groups: Mapping[str, Any] | None = None) -> list[str]:
    deps: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        deps += extra
    for group in (groups or {}).values():
        deps += [d for d in group if isinstance(d, str)]
    return deps


def sibling_versions(fs: FileSystem, root: Path, pattern: str) -> list[Violation]:
    projects: dict[str, tuple[str, Path, Mapping[str, Any]]] = {}
    for path in fs.glob(root, pattern):
        data = tomllib.loads(fs.read_text(path))
        project = data.get("project", {})
        if "name" in project:
            projects[dist_name(project["name"])] = (str(project.get("version", "0")), path, data)
    out: list[Violation] = []
    for name, (_, path, doc) in projects.items():
        for dep in _dependencies(doc.get("project", {}), doc.get("dependency-groups")):
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*([^;]*)", dep)
            if not m or dist_name(m.group(1)) not in projects or dist_name(m.group(1)) == name:
                continue
            sibling, spec = dist_name(m.group(1)), m.group(2).strip()
            version = projects[sibling][0]
            if spec and not _satisfies(version, spec):
                rel = path.relative_to(root).as_posix()
                out.append(Violation(Rule.SIBLING_VERSIONS, rel, f"requires {sibling}{spec} but the sibling in this repository is {version}"))
    return out


def secrets_guard(fs: FileSystem, root: Path, tests_dir: str) -> list[Violation]:
    tests = root / tests_dir
    if not fs.is_dir(tests):
        return []
    conftest = tests / "conftest.py"
    rel = f"{tests_dir}/conftest.py"
    if not fs.is_file(conftest):
        return [Violation(Rule.SECRETS_GUARD, rel, "no conftest.py: tests can read the developer's real secrets from the environment")]
    text = fs.read_text(conftest)
    if not (re.search(r"autouse\s*=\s*True", text) and re.search(r"delenv|environ", text)):
        return [Violation(Rule.SECRETS_GUARD, rel, "conftest.py has no autouse fixture that scrubs the environment (monkeypatch.delenv / os.environ)")]
    return []


def docs_sync(
    fs: FileSystem, root: Path, doc: str, policy: Policy, scan: ScanResult,
) -> list[Violation]:
    path = root / doc
    if not fs.is_file(path):
        return [Violation(Rule.DOCS_SYNC, doc, "documented architecture file does not exist")]
    rows: dict[str, str] = {}
    for line in fs.read_text(path).splitlines():
        m = re.match(r"\|\s*`([^`]+)`\s*\|\s*`?([\w-]+)`?\s*\|", line)
        if m:
            mod = m.group(1).removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            rows[mod] = m.group(2)
    out: list[Violation] = []
    for mod, ring in sorted(rows.items()):
        if mod not in scan.known:
            out.append(Violation(Rule.DOCS_SYNC, mod, f"{doc} lists a module that does not exist"))
        elif policy.resolve_ring(scan.arbor_index, mod) != ring:
            out.append(Violation(Rule.DOCS_SYNC, mod, f"{doc} says ring {ring!r} but the policy says {policy.resolve_ring(scan.arbor_index, mod)!r}"))
    out += [Violation(Rule.DOCS_SYNC, m, f"module is not in the {doc} table") for m in sorted(set(scan.known) - set(rows))]
    return out


def crlf(fs: FileSystem, root: Path, patterns: Sequence[str]) -> list[Violation]:
    out: list[Violation] = []
    for path in fs.rglob(root, "*"):
        rel = path.relative_to(root).as_posix()
        if _NOISE & set(rel.split("/")) or ".git" in rel.split("/") or not fs.is_file(path):
            continue
        if any(fnmatch.fnmatch(path.name, p) for p in patterns) and b"\r\n" in fs.read_bytes(path):
            out.append(Violation(Rule.CRLF, rel, "has CRLF line endings; the interpreter line becomes '/bin/sh\\r'"))
    return out


def undeclared_imports(
    fs: FileSystem, root: Path, policy: Policy, modules: Mapping[str, ModuleInfo], known: Collection[str], manifest: str
) -> list[Violation]:
    declared: set[str] = set()
    path = root / manifest
    if fs.is_file(path):
        data = tomllib.loads(fs.read_text(path))
        declared = {
            dist_name(re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0])
            for d in _dependencies(data.get("project", {}), data.get("dependency-groups"))
        }
    tops = {m.split(".")[0] for m in known}
    stdlib = policy.stdlib
    out: dict[str, Violation] = {}
    for name, info in modules.items():
        for imp in info.facts.imports:
            for top in resolve_targets(name, info.is_package, imp, known, tops)[1]:
                dist = dist_name(policy.dependencies.get(top, top))
                if top not in stdlib and dist not in declared:
                    out[dist] = Violation(Rule.UNDECLARED_IMPORTS, dist, f"imported (e.g. by {name}) but not declared in {manifest}", imp.line)
    return list(out.values())


PRACTICES = (
    Rule.GITIGNORE_SHADOW, Rule.SIBLING_VERSIONS, Rule.SECRETS_GUARD, Rule.DOCS_SYNC, Rule.CRLF, Rule.UNDECLARED_IMPORTS
)  # fmt: skip


def run_practices(
    policy: Policy, root: Path, runner: ToolRunner, fs: FileSystem, scan: ScanResult,
) -> list[Violation]:
    modules = scan.modules
    known = scan.known
    cfg = policy.practices
    out: list[Violation] = []
    if cfg.get(Rule.GITIGNORE_SHADOW):
        value = cfg[Rule.GITIGNORE_SHADOW]
        items = cast("list[object]", value) if isinstance(value, list) else []
        allow = [str(a) for a in items]
        out += gitignore_shadow(root, runner, allow)
    if cfg.get(Rule.SIBLING_VERSIONS):
        out += sibling_versions(fs, root, str(cfg[Rule.SIBLING_VERSIONS]))
    if cfg.get(Rule.SECRETS_GUARD):
        out += secrets_guard(fs, root, str(cfg[Rule.SECRETS_GUARD]))
    if cfg.get(Rule.DOCS_SYNC):
        out += docs_sync(fs, root, str(cfg[Rule.DOCS_SYNC]), policy, scan)
    if cfg.get(Rule.CRLF):
        out += crlf(fs, root, list(cfg[Rule.CRLF]))
    if cfg.get(Rule.UNDECLARED_IMPORTS):
        manifest = cfg[Rule.UNDECLARED_IMPORTS] if isinstance(cfg[Rule.UNDECLARED_IMPORTS], str) else "pyproject.toml"
        out += undeclared_imports(fs, root, policy, modules, known, manifest)
    return out
