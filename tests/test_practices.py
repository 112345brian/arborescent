"""Each repository-hygiene practice reproduces the real bug in a temp repository, then shows the rule catches it.

The bugs are the ones met while building DeepInterview.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SubprocessToolRunner
from arborescent.practices import (
    crlf,
    docs_sync,
    gitignore_shadow,
    secrets_guard,
    sibling_versions,
    undeclared_imports,
)  # fmt: skip

FS = LocalFileSystem()
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@needs_git
def test_a_gitignore_pattern_that_matches_a_package_directory_is_caught(tmp_path: Path) -> None:
    """Real bug: an unanchored `local_voice/` ignored services/local-voice/src/local_voice/ -> empty wheel."""
    _ = subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_files(
        tmp_path,
        {
            ".gitignore": "local_voice/\n__pycache__/\n",
            "services/local-voice/src/local_voice/__init__.py": "",
            "services/local-voice/src/local_voice/server.py": "X = 1\n",
            "services/local-voice/src/local_voice/__pycache__/server.cpython-311.py": "",
            "services/other/src/other/__init__.py": "",
        },
    )
    found = gitignore_shadow(tmp_path, SubprocessToolRunner())
    assert [v.subject for v in found] == ["services/local-voice/src/local_voice"]
    assert "local_voice/" in found[0].detail


@needs_git
def test_an_anchored_pattern_is_fine(tmp_path: Path) -> None:
    _ = subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_files(tmp_path, {".gitignore": "/local_voice/\n", "services/local-voice/src/local_voice/__init__.py": ""})
    assert gitignore_shadow(tmp_path, SubprocessToolRunner()) == []


def test_gitignore_shadow_is_silent_outside_a_git_repository(tmp_path: Path) -> None:
    write_files(tmp_path, {"a/b.py": ""})
    # a plain folder (no .git): git exits non-zero, the rule reports nothing rather than guessing
    if shutil.which("git"):
        assert gitignore_shadow(tmp_path, SubprocessToolRunner()) == []


def _dist(name: str, version: str, deps: list[str]) -> str:
    listed = ", ".join(f'"{d}"' for d in deps)
    return f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = [{listed}]\n'


def test_sibling_distributions_pinned_to_inconsistent_versions_are_caught(tmp_path: Path) -> None:
    """Real bug: the launch-runner meta-package pinned ==0.1.1 while the components were 0.2.0."""
    write_files(
        tmp_path,
        {
            "distributions/proclaunch/pyproject.toml": _dist("proclaunch", "0.2.0", []),
            "distributions/lifecycle-runner/pyproject.toml": _dist("lifecycle-runner", "0.2.0", ["psutil>=5"]),
            "distributions/launch-runner/pyproject.toml": _dist(
                "launch-runner", "0.2.0", ["proclaunch==0.1.1", "lifecycle_runner==0.2.0"]
            ),
        },
    )
    found = sibling_versions(FS, tmp_path, "distributions/*/pyproject.toml")
    assert [(v.subject, v.rule) for v in found] == [("distributions/launch-runner/pyproject.toml", "sibling-versions")]
    assert "proclaunch==0.1.1" in found[0].detail and "0.2.0" in found[0].detail


@pytest.mark.parametrize(
    ("spec", "ok"),
    [("==0.2.0", True), (">=0.2", True), (">=0.3", False), ("~=0.2.0", True), ("~=0.1.0", False), ("<0.2", False),
     ("==0.2.*", True), ("!=0.2.0", False), (">=0.1,<0.3", True), ("", True)],
)  # fmt: skip
def test_version_specifier_evaluation(tmp_path: Path, spec: str, ok: bool) -> None:
    write_files(
        tmp_path,
        {"d/a/pyproject.toml": _dist("a", "0.2.0", []), "d/b/pyproject.toml": _dist("b", "1.0.0", [f"a{spec}"])},
    )
    assert (sibling_versions(FS, tmp_path, "d/*/pyproject.toml") == []) is ok


def test_tests_that_can_read_real_secrets_need_a_conftest_guard(tmp_path: Path) -> None:
    """Real bug: a test run picked up the developer's real API key from the environment."""
    write_files(tmp_path, {"tests/test_x.py": "import os\n\ndef test_x():\n    assert os.environ.get('ANTHROPIC_API_KEY')\n"})
    assert [v.rule for v in secrets_guard(FS, tmp_path, "tests")] == ["secrets-guard"]
    write_files(tmp_path, {"tests/conftest.py": "import pytest\n\n@pytest.fixture\ndef nothing():\n    pass\n"})
    assert [v.rule for v in secrets_guard(FS, tmp_path, "tests")] == ["secrets-guard"]  # a conftest alone is not a guard
    write_files(
        tmp_path,
        {"tests/conftest.py": "import pytest\n\n@pytest.fixture(autouse=True)\ndef scrub(monkeypatch):\n    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)\n"},
    )
    assert secrets_guard(FS, tmp_path, "tests") == []


def test_docs_that_drift_from_the_policy_are_caught(repo: Path) -> None:
    """Real bug: the architecture doc, the tach config and the tests disagreed about a module's layer."""
    policy = mini_policy()
    known = FsScanner().scan(policy, repo).known
    table = "| module | ring |\n|---|---|\n" + "".join(
        f"| `{m}` | {policy.ring_of(m)} |\n" for m in sorted(known)
    )
    write_files(repo, {"docs/architecture.md": table})
    assert docs_sync(FS, repo, "docs/architecture.md", policy, known) == []
    drifted = table.replace("| `app.adapters` | adapters |", "| `app.adapters` | ports |").replace("| `app.main` | composition |\n", "") + "| `app.gone` | domain |\n"
    write_files(repo, {"docs/architecture.md": drifted})
    details = sorted(v.detail for v in docs_sync(FS, repo, "docs/architecture.md", policy, known))
    assert any("says ring 'ports'" in d for d in details)
    assert any("does not exist" in d for d in details)
    assert any("not in the docs/architecture.md table" in d for d in details)


def test_generated_scripts_with_crlf_are_caught(tmp_path: Path) -> None:
    """Real bug: a script written on Windows with CRLF failed with `/bin/sh^M: bad interpreter`."""
    (tmp_path / "run.sh").write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    (tmp_path / "ok.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    (tmp_path / "notes.txt").write_bytes(b"a\r\nb\r\n")
    assert [v.subject for v in crlf(FS, tmp_path, ["*.sh"])] == ["run.sh"]


def test_imports_of_undeclared_dependencies_are_caught(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": "import psutil\nimport requests\n", "pyproject.toml": _dist("app", "1", ["psutil>=5"])})
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    found = undeclared_imports(FS, repo, policy, scan.modules, scan.known, "pyproject.toml")
    assert [(v.rule, v.subject) for v in found] == [("undeclared-imports", "requests")]


def test_import_name_to_distribution_mapping_is_honoured(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": "import yaml\n", "pyproject.toml": _dist("app", "1", ["PyYAML"])})
    policy = mini_policy(dependencies={"yaml": "pyyaml"})
    scan = FsScanner().scan(policy, repo)
    assert undeclared_imports(FS, repo, policy, scan.modules, scan.known, "pyproject.toml") == []
