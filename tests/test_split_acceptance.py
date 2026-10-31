"""#57: annotations alone drive a split. External tools (git filter-repo, import-linter, tach) are the harness; Arborescent does not migrate.

The flow, on the synthetic monorepo of ``split_fixture``:

1. the engines' own output (generated contracts, #53) is the cut list of the blocked package;
2. the JSON export (#54) is the only input the splitting harness reads; ``git filter-repo`` makes one history-preserving
   clone per package from the export's ``path_renames``;
3. each clone is dressed as a part and checked with the annotation-preservation rules (#55).

Skipped, with the reason, where an external tool is not installed. No network is used.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import split_fixture as fixture
from arborescent_helpers import services

from arborescent.adapters import LocalFileSystem
from arborescent.cli import main
from arborescent.config import load_policy
from arborescent.domain import Rule
from arborescent.engine import run_own
from arborescent.package_model import MANIFEST_VERSION
from arborescent.testing import FakeToolRunner

pytestmark = pytest.mark.external_tools

PRESERVATION_RULES = {
    Rule.PACKAGE_SEMANTICS_DRIFT, Rule.PACKAGE_MEMBERSHIP_DRIFT, Rule.PACKAGE_RING_DRIFT,
    Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED, Rule.PACKAGE_PRIVATE_IMPORT,
}
BLOCKED_MESSAGE = "app.orders.service is not allowed to import app.billing.internal"
PACKAGES = ("kernel", "billing", "orders", "shell")


def _git_env() -> dict[str, str]:
    """A hermetic git: no user or system config, no inherited GIT_CONFIG_* (the conftest scrubs half of such a set)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    return env


def run(argv: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(argv, cwd=cwd, env=_git_env(), capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise AssertionError(f"{argv} failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
    return done


def _have_filter_repo() -> bool:
    return shutil.which("git") is not None and run(["git", "filter-repo", "--version"], Path.cwd(), check=False).returncode == 0


needs_import_linter = pytest.mark.skipif(shutil.which("lint-imports") is None, reason="import-linter not installed (lint-imports not on PATH)")
needs_tach = pytest.mark.skipif(shutil.which("tach") is None, reason="tach not installed (tach not on PATH)")
needs_filter_repo = pytest.mark.skipif(not _have_filter_repo(), reason="git filter-repo not installed (pip install git-filter-repo)")


def arborescent(root: Path, *args: str) -> int:
    return main(["--config", str(root / "arborescent.toml"), *args])


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def commit(root: Path, message: str) -> None:
    run(["git", "add", "-A"], root)
    run(["git", "commit", "-q", "-m", message], root)


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    """The monorepo with its generated engine config and a git history; nothing outside ``tmp_path`` is touched."""
    root = tmp_path / "mono"
    root.mkdir()
    write(root, {**fixture.FILES, "arborescent.toml": fixture.POLICY, "packages.toml": fixture.CATALOG})
    run(["git", "init", "-q", "-b", "main"], root)
    (root / ".gitignore").write_text("/migration.json\n/ruff.toml\n/ty.toml\n__pycache__/\n", encoding="utf-8")
    commit(root, "initial monorepo")
    write(root, {"app/billing/models.py": fixture.FILES["app/billing/models.py"] + "\n\nTAX = 7\n"})
    commit(root, "billing: add TAX")
    assert arborescent(root, "generate") == 0
    commit(root, "generated engine config")
    return root


def export(root: Path) -> dict[str, Any]:
    assert arborescent(root, "packages", "export", "--output", "migration.json") == 0
    return json.loads((root / "migration.json").read_text(encoding="utf-8"))


def by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {package["id"]: package for package in document["packages"]}


def remove_the_blocking_edge(root: Path) -> None:
    path = root / "app/orders/service.py"
    path.write_text(path.read_text(encoding="utf-8").replace(fixture.BLOCKING_IMPORT, fixture.CLEAN_IMPORT), encoding="utf-8", newline="\n")
    commit(root, "orders: use billing's public api")


def own_findings(root: Path, *, parts: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    policy, _ = load_policy(LocalFileSystem(), root / "arborescent.toml")
    if parts:
        policy = dataclasses.replace(policy, package_parts=parts)
    found, _ = run_own(policy, root, services(FakeToolRunner()))
    return sorted((v.rule, v.subject) for v in found if v.rule not in {Rule.PACKAGE_SPANS_RINGS})


# --- 1. the cut list is the engines' own output ---------------------------------------------------------------------


@needs_import_linter
def test_import_linter_lists_the_blocking_edge_of_the_blocked_package_and_nothing_else(monorepo: Path) -> None:
    broken = run(["lint-imports", "--config", "arborescent-importlinter.toml"], monorepo, check=False)
    assert broken.returncode != 0
    assert "package orders imports only declared packages' public modules BROKEN" in broken.stdout
    assert BLOCKED_MESSAGE in broken.stdout
    for other in ("kernel", "billing", "shell"):
        assert f"package {other} imports only declared packages' public modules KEPT" in broken.stdout
    assert len(re.findall(r"is not allowed to import", broken.stdout)) == 1  # the whole cut list is one edge


@needs_tach
def test_tach_names_the_same_edge(monorepo: Path) -> None:
    broken = run(["tach", "check", "--dependencies"], monorepo, check=False)
    assert broken.returncode != 0
    assert "app/orders/service.py" in broken.stdout + broken.stderr and "app.billing.internal" in broken.stdout + broken.stderr


@needs_import_linter
@needs_tach
def test_removing_the_edge_makes_every_package_clean_for_both_engines(monorepo: Path) -> None:
    remove_the_blocking_edge(monorepo)
    assert run(["lint-imports", "--config", "arborescent-importlinter.toml"], monorepo, check=False).returncode == 0
    assert run(["tach", "check", "--dependencies"], monorepo, check=False).returncode == 0
    assert own_findings(monorepo) == []
    assert by_id(export(monorepo))["billing"]["surface"]["bypasses"] == []


def test_the_export_reports_the_edge_as_a_bypass_of_billings_surface(monorepo: Path) -> None:
    document = export(monorepo)
    assert by_id(document)["billing"]["surface"]["bypasses"] == [
        {"imports": "app.billing.internal", "module": "app.orders.service", "package": "orders"}
    ]
    assert (Rule.PACKAGE_PRIVATE_IMPORT, "app.orders.service") in own_findings(monorepo)


def test_the_export_validates_against_its_published_schema(monorepo: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(subprocess.run(
        [os.sys.executable, "-m", "arborescent", "--config", str(monorepo / "arborescent.toml"), "packages", "schema"],
        capture_output=True, text=True, check=True).stdout)
    jsonschema.Draft202012Validator(schema).validate(export(monorepo))


# --- 2 and 3. the split, from the export alone ----------------------------------------------------------------------


def split(monorepo: Path, document: dict[str, Any], pid: str) -> Path:
    """One history-preserving clone of the monorepo per package, keeping and renaming what the export says (the documented recipe)."""
    package = by_id(document)[pid]
    work = monorepo.parent / "work" / pid
    run(["git", "clone", "-q", "--no-local", str(monorepo), str(work)], monorepo.parent)
    lines = [rename["source"] for rename in package["owned"]["path_renames"]]
    lines += [
        f"{rename['source']}/==>{rename['target']}/" if (work / rename["source"]).is_dir() else f"{rename['source']}==>{rename['target']}"
        for rename in package["owned"]["path_renames"]
    ]
    listing = monorepo.parent / "work" / f"{pid}.paths"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(["git", "filter-repo", "--force", "--paths-from-file", str(listing)], work)
    return work


def member_root(work: Path, package: dict[str, Any]) -> Path:
    """Where the package's own tree begins: the repository, or its folder in the workspace."""
    home = package["home"]
    return work / home["path"] if home["kind"] == "workspace-member" else work


def dress_as_part(monorepo: Path, document: dict[str, Any], pid: str, tree: Path) -> str:
    """Add what a part needs to be checkable by #55 and the export does not carry: a manifest and a policy (schema gaps)."""
    package = by_id(document)[pid]
    owned = package["owned"]
    modules = {m["module"] for m in owned["modules"]}
    origin_paths = [m["path"] for m in owned["modules"]] + [r["source"] for r in owned["path_renames"] if r["source"] not in {m["path"] for m in owned["modules"]}]
    policy_lines = []
    for line in fixture.POLICY.splitlines():
        match = re.fullmatch(r'"([^"]+)" = "[^"]+"', line)
        if line.startswith("package_catalog") or (match and match.group(1) not in modules):
            continue
        policy_lines.append(line)
    (tree / "arborescent.toml").write_text("\n".join(policy_lines) + "\n", encoding="utf-8", newline="\n")
    surface = package["surface"]["public"]
    manifest = [
        f"manifest_version = {MANIFEST_VERSION}", "", "[package]",
        f'id = "{pid}"', f'name = "{package["name"]}"', f'kind = "{package["kind"]}"', f'summary = "{package["summary"]}"',
        f"capabilities = {json.dumps(package['capabilities'])}", f"depends_on = {json.dumps(package['dependencies']['packages'])}",
        f"public = {json.dumps(surface)}" if surface else "", "",
        "[origin]", 'repository = "example/monorepo"', 'root = "."', 'extraction_status = "in-repository"', "",
        "[location]", f'repository = "{package["home"].get("repository") or "example/monorepo"}"', 'root = "."', "",
        "[extraction]", 'status = "extracted"', f'included_packages = ["{pid}"]', "",
    ]
    targets = {r["source"]: r["target"] for r in owned["path_renames"]}
    prefix = package["home"]["path"] + "/" if package["home"]["kind"] == "workspace-member" else ""
    for origin in origin_paths:
        role = "assets" if origin not in {m["path"] for m in owned["modules"]} else "source"
        manifest += ["[[paths]]", f'package_id = "{pid}"', f'role = "{role}"', "metadata = {}",
                     f'origin_path = "{origin}"', f'current_path = "{targets[origin].removeprefix(prefix)}"', ""]
    (tree / ".arborescent-package.toml").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
    return f"split/{pid}"


@pytest.fixture
def split_monorepo(monorepo: Path) -> tuple[Path, dict[str, Any], dict[str, Path], tuple[str, ...]]:
    remove_the_blocking_edge(monorepo)
    document = export(monorepo)
    assert document["ready"], document["gaps"]
    works: dict[str, Path] = {}
    locations: list[str] = []
    for pid in PACKAGES:
        works[pid] = split(monorepo, document, pid)
        tree = member_root(works[pid], by_id(document)[pid])
        part = monorepo / "split" / pid
        shutil.copytree(tree, part, ignore=shutil.ignore_patterns(".git"))
        locations.append(dress_as_part(monorepo, document, pid, part))
    return monorepo, document, works, tuple(locations)


@needs_filter_repo
def test_each_clone_holds_exactly_what_the_export_assigns_to_its_package(split_monorepo: Any) -> None:
    _, document, works, _ = split_monorepo
    for pid, work in works.items():
        expected = {item["target_path"] for item in by_id(document)[pid]["owned"]["modules"]}
        expected |= {item["target_path"] for item in by_id(document)[pid]["owned"]["assets"]}
        held = set(run(["git", "ls-files"], work).stdout.split())
        assert held == expected, pid


@needs_filter_repo
def test_history_is_kept_for_a_package_in_its_own_repository_and_for_a_workspace_member(split_monorepo: Any) -> None:
    _, document, works, _ = split_monorepo
    billing = by_id(document)["billing"]["owned"]["modules"]
    target = next(m["target_path"] for m in billing if m["module"] == "app.billing.models")
    log = run(["git", "log", "--format=%s", "--", target], works["billing"]).stdout.split("\n")
    assert "billing: add TAX" in log and "initial monorepo" in log  # the workspace member
    assert run(["git", "log", "--format=%s"], works["kernel"]).stdout.split("\n")[:-1] == ["initial monorepo"]  # the own-repo package: only its commits remain
    for pid, work in works.items():  # no other package's file anywhere in a clone's history
        ever = set(run(["git", "log", "--all", "--name-only", "--format="], work).stdout.split())
        assert not any(f"app/{other}/" in name for other in PACKAGES if other != pid for name in ever), pid


@needs_filter_repo
def test_a_faithful_split_has_no_preservation_findings(split_monorepo: Any) -> None:
    monorepo, _, _, locations = split_monorepo
    assert own_findings(monorepo, parts=locations) == []


@needs_filter_repo
def test_the_preservation_checks_catch_a_part_that_lost_a_public_symbol(split_monorepo: Any) -> None:
    monorepo, _, _, locations = split_monorepo
    models = monorepo / "split/billing/app/billing/models.py"
    models.write_text(models.read_text(encoding="utf-8").replace("class Invoice:\n    amount: types.Money = 0\n", ""), encoding="utf-8", newline="\n")
    assert (Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED, "app.billing.models") in own_findings(monorepo, parts=locations)


@needs_filter_repo
def test_the_preservation_checks_catch_a_part_that_reaches_a_private_module(split_monorepo: Any) -> None:
    monorepo, _, _, locations = split_monorepo
    service = monorepo / "split/orders/app/orders/service.py"
    service.write_text(service.read_text(encoding="utf-8").replace(fixture.CLEAN_IMPORT, fixture.BLOCKING_IMPORT), encoding="utf-8", newline="\n")
    assert (Rule.PACKAGE_PRIVATE_IMPORT, "app.orders.service") in own_findings(monorepo, parts=locations)

