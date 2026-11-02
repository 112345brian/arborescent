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
from arborescent.config import policy_from_mapping
from arborescent.practices import (
    crlf,
    docs_sync,
    gitignore_shadow,
    run_practices,
    secrets_guard,
    sibling_versions,
    source_root_drift,
    stale_config_paths,
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


def test_docs_sync_uses_each_scope_ring(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/alpha/core.py": "VALUE = 1\n",
        "libs/beta/src/beta/api.py": "VALUE = 2\n",
        "architecture.md": (
            "| module | scope | ring |\n|---|---|---|\n"
            "| `alpha.core` | `alpha` | `domain` |\n| `beta.api` | `beta` | `adapters` |\n"
        ),
    })
    policy = policy_from_mapping({
        "practices": {"docs-sync": "architecture.md"},
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain", "adapters"], "classify": {"alpha.core": "domain"},
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain", "adapters"], "classify": {"beta.api": "domain"},
            },
        },
    })
    scan = FsScanner().scan(policy, tmp_path)

    found = docs_sync(FS, tmp_path, "architecture.md", policy, scan)

    assert [(item.subject, item.rule) for item in found] == [("beta.api", "docs-sync")]


def test_docs_sync_checks_scope_ownership_when_documented(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/alpha/core.py": "VALUE = 1\n",
        "architecture.md": "| `alpha.core` | `beta` | `domain` |\n",
    })
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain"], "classify": {"alpha.core": "domain"},
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain"], "classify": {"beta.api": "domain"},
            },
        },
    })
    scan = FsScanner().scan(policy, tmp_path)

    found = docs_sync(FS, tmp_path, "architecture.md", policy, scan)

    assert "scope 'beta'" in found[0].detail


def test_docs_that_drift_from_the_policy_are_caught(repo: Path) -> None:
    """Real bug: the architecture doc, the tach config and the tests disagreed about a module's layer."""
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    known = scan.known
    table = "| module | ring |\n|---|---|\n" + "".join(
        f"| `{m}` | {policy.ring_of(m)} |\n" for m in sorted(known)
    )
    write_files(repo, {"docs/architecture.md": table})
    assert docs_sync(FS, repo, "docs/architecture.md", policy, scan) == []
    drifted = table.replace("| `app.adapters` | adapters |", "| `app.adapters` | ports |").replace("| `app.main` | composition |\n", "") + "| `app.gone` | domain |\n"
    write_files(repo, {"docs/architecture.md": drifted})
    details = sorted(v.detail for v in docs_sync(FS, repo, "docs/architecture.md", policy, scan))
    assert any("says ring 'ports'" in d for d in details)
    assert any("does not exist" in d for d in details)
    assert any("not in the docs/architecture.md table" in d for d in details)


@pytest.mark.parametrize("documented_ring", ["domain", "ports"])
def test_docs_sync_uses_the_annotation_ring(tmp_path: Path, documented_ring: str) -> None:
    write_files(
        tmp_path,
        {
            "app/model.py": '__arbor__ = {"ring": "ports"}\n',
            "docs/architecture.md": f"| `app.model` | {documented_ring} |\n",
        },
    )
    policy = mini_policy(classify={"app.model": "domain"}, practices={"docs-sync": "docs/architecture.md"})
    scan = FsScanner().scan(policy, tmp_path)

    found = run_practices(policy, tmp_path, SubprocessToolRunner(), FS, scan)

    if documented_ring == "ports":
        assert found == []
    else:
        assert [(v.rule, v.subject) for v in found] == [("docs-sync", "app.model")]
        assert "says ring 'domain' but the policy says 'ports'" in found[0].detail


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


def test_scope_import_name_to_distribution_mapping_is_honoured(repo: Path) -> None:
    write_files(repo, {
        "libs/alpha/src/alpha/core.py": "import yaml\n",
        "pyproject.toml": _dist("app", "1", ["PyYAML"]),
    })
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain"], "dependencies": {"yaml": "pyyaml"},
            },
        },
    })
    scan = FsScanner().scan(policy, repo)
    assert undeclared_imports(FS, repo, policy, scan.modules, scan.known, "pyproject.toml") == []


# -- stale-config-path (#89) --------------------------------------------------------------------------------------------

MAKEFILE = "check:\n\tfor f in scripts/test_*.py; do python $$f; done\n\tpytest $(wildcard scripts/test_*.py)\n"


def test_a_glob_in_build_config_that_matches_nothing_is_caught(tmp_path: Path) -> None:
    """Real bug: a migration dissolved scripts/; `make check-ci` kept passing while running zero tests."""
    write_files(tmp_path, {"Makefile": MAKEFILE})
    found = stale_config_paths(FS, tmp_path, ["Makefile"])
    assert [(v.rule, v.subject, v.line) for v in found] == [("stale-config-path", "Makefile", 2)]
    assert "scripts/test_*.py" in found[0].detail


def test_a_glob_that_still_matches_is_clean(tmp_path: Path) -> None:
    write_files(tmp_path, {"Makefile": MAKEFILE, "scripts/test_a.py": ""})
    assert stale_config_paths(FS, tmp_path, ["Makefile"]) == []


def test_a_missing_script_and_a_missing_hatch_package_are_caught(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "Makefile": "gen:\n\tpython tools/gen.py\n",
        "pyproject.toml": '[tool.hatch.build.targets.wheel]\npackages = ["scripts", "src/app"]\n',
        "src/app/__init__.py": "",
    })
    found = stale_config_paths(FS, tmp_path, ["Makefile", "pyproject.toml"])
    assert [(v.subject, "tools/gen.py" in v.detail or "scripts" in v.detail) for v in found] == [
        ("Makefile", True), ("pyproject.toml", True),
    ]
    write_files(tmp_path, {"tools/gen.py": "", "scripts/x.py": ""})
    assert stale_config_paths(FS, tmp_path, ["Makefile", "pyproject.toml"]) == []


def test_urls_variables_generated_folders_comments_and_absolute_paths_are_not_references(tmp_path: Path) -> None:
    write_files(tmp_path, {"Makefile": (
        "# old: scripts/test_*.py\nclean:\n\trm -rf build/* dist/*.whl .venv/lib/*.py\n"
        "\tcurl https://example.com/a/b.py\n\tls $(OUT)/x/*.py /usr/bin/*.py ../other/*.py ~/z/*.py\n"
        "\tfind . -name '*.pyc'\n\tdelete src/**/*.pyc\n"
    )})
    assert stale_config_paths(FS, tmp_path, ["Makefile"]) == []


def test_the_allow_list_and_unmatched_config_globs_are_quiet(tmp_path: Path) -> None:
    write_files(tmp_path, {"Makefile": MAKEFILE})
    assert stale_config_paths(FS, tmp_path, ["Makefile"], ["scripts/*"]) == []
    assert stale_config_paths(FS, tmp_path, ["Missing", ".github/workflows/*.yml"]) == []


def test_each_stale_token_is_reported_once_per_file(tmp_path: Path) -> None:
    write_files(tmp_path, {"Makefile": MAKEFILE + "again:\n\tls scripts/test_*.py\n"})
    assert len(stale_config_paths(FS, tmp_path, ["Makefile"])) == 1


# -- source-root-drift (#91) --------------------------------------------------------------------------------------------

def _drift(repo: Path, files: list[str] | None = None, **overrides: object) -> list[tuple[str, str]]:
    policy = mini_policy(**overrides)
    return [(v.subject, v.detail) for v in source_root_drift(FS, repo, policy, files or ["tach.toml", "pyproject.toml"])]


def test_a_source_outside_every_source_root_is_caught(tmp_path: Path) -> None:
    found = _drift(tmp_path, sources=["libs/x/src/x"], source_roots=["libs/y/src"])
    assert len(found) == 1 and "libs/x/src/x" in found[0][1] and "source_root" in found[0][1]
    assert _drift(tmp_path, sources=["libs/x/src/x"], source_roots=["libs/x/src"]) == []


def test_tach_source_roots_must_equal_the_policys(tmp_path: Path) -> None:
    write_files(tmp_path, {"tach.toml": 'source_roots = ["./libs/a/src/", "libs/old/src"]\n'})
    found = _drift(tmp_path, sources=["libs/a/src/a", "libs/b/src/b"], source_roots=["libs/a/src", "libs/b/src"])
    assert {s for s, _ in found} == {"tach.toml"}
    text = " ".join(d for _, d in found)
    assert "libs/b/src" in text and "libs/old/src" in text and "libs/a/src'" not in text
    write_files(tmp_path, {"tach.toml": 'source_roots = ["libs/b/src", "./libs/a/src"]\n'})
    assert _drift(tmp_path, sources=["libs/a/src/a", "libs/b/src/b"], source_roots=["libs/a/src", "libs/b/src"]) == []


def test_pyproject_hatch_and_pytest_lists_must_carry_every_root_and_source(tmp_path: Path) -> None:
    roots, sources = ["libs/a/src", "libs/b/src"], ["libs/a/src/a", "libs/b/src/b"]
    write_files(tmp_path, {"pyproject.toml": (
        '[tool.hatch.build.targets.wheel]\nsources = ["libs/a/src"]\npackages = ["libs/a/src/a"]\n'
        '[tool.pytest.ini_options]\npythonpath = ["libs/a/src"]\n'
    )})
    text = " ".join(d for _, d in _drift(tmp_path, sources=sources, source_roots=roots))
    assert text.count("libs/b/src") >= 3 and "libs/b/src/b" in text
    write_files(tmp_path, {"pyproject.toml": (
        '[tool.hatch.build.targets.wheel]\nsources = {"libs/a/src" = "", "libs/b/src" = ""}\npackages = ["libs/a/src/a", "libs/b/src/b"]\n'
        '[tool.pytest.ini_options]\npythonpath = [".", "libs/a/src", "libs/b/src"]\n'
    )})
    assert _drift(tmp_path, sources=sources, source_roots=roots) == []


def test_lists_that_are_absent_and_files_that_are_missing_are_not_drift(tmp_path: Path) -> None:
    assert _drift(tmp_path, sources=["app"], source_roots=["."]) == []
    write_files(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    assert _drift(tmp_path, sources=["app"], source_roots=["."]) == []


def test_scopes_contribute_the_union_of_their_roots_and_sources(tmp_path: Path) -> None:
    scope = {"rings": ["domain"], "classify": {}}
    policy = policy_from_mapping({"scopes": {
        "a": {**scope, "sources": ["a/src/a"], "source_roots": ["a/src"]},
        "b": {**scope, "sources": ["b/src/b"], "source_roots": ["b/src"]},
    }})
    write_files(tmp_path, {"tach.toml": 'source_roots = ["a/src"]\n'})
    found = source_root_drift(FS, tmp_path, policy, ["tach.toml"])
    assert [v.subject for v in found] == ["tach.toml"] and "b/src" in found[0].detail


def test_both_practices_run_from_the_policy(repo: Path) -> None:
    write_files(repo, {"Makefile": MAKEFILE, "tach.toml": 'source_roots = ["elsewhere"]\n'})
    policy = mini_policy(practices={"stale-config-path": ["Makefile"], "source-root-drift": True})
    scan = FsScanner().scan(policy, repo)
    found = run_practices(policy, repo, SubprocessToolRunner(), FS, scan)
    assert {v.rule for v in found} == {"stale-config-path", "source-root-drift"}
    off = run_practices(mini_policy(), repo, SubprocessToolRunner(), FS, scan)
    assert not {v.rule for v in off} & {"stale-config-path", "source-root-drift"}
