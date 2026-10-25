"""Characterization of every file access in the application ring, written BEFORE the FileSystem port existed.

These pin what baseline, config, the practices, selftest and bench do with the file system today (including the edge
inputs: missing, empty, CRLF, non-UTF-8, a directory where a file should be), so routing the I/O through a port
cannot change them silently.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from archlint_helpers import MINI_FILES, mini_policy, write_files

from archlint import baseline
from archlint.adapters import FsScanner
from archlint.bench import bench_one
from archlint.config import find_policy, load_policy
from archlint.domain import PolicyError, Violation
from archlint.engine import Services, drift
from archlint.generate import generate
from archlint.practices import crlf, docs_sync, secrets_guard, sibling_versions, undeclared_imports
from archlint.selftest import selftest
from archlint.testing import FakeToolRunner
from archlint.typecheckers import all_checkers, by_name

CHECKERS = list(all_checkers().values())
V = Violation("internal-import", "a.b", "msg")


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --- baseline --------------------------------------------------------------------------------------------------


def test_baseline_load_of_a_missing_file_or_a_directory_is_empty(tmp_path: Path) -> None:
    assert baseline.load(tmp_path / "nope.json") == set()
    (tmp_path / "dir").mkdir()
    assert baseline.load(tmp_path / "dir") == set()


def test_baseline_load_reads_crlf_and_a_missing_violations_key(tmp_path: Path) -> None:
    (tmp_path / "b.json").write_bytes(b'{\r\n  "violations": ["x|y"]\r\n}\r\n')
    assert baseline.load(tmp_path / "b.json") == {"x|y"}
    (tmp_path / "c.json").write_text("{}", encoding="utf-8")
    assert baseline.load(tmp_path / "c.json") == set()


def test_baseline_load_rejects_empty_and_non_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "empty.json").write_bytes(b"")
    with pytest.raises(ValueError):
        baseline.load(tmp_path / "empty.json")
    (tmp_path / "bad.json").write_bytes(b'{"violations": ["\xff"]}')
    with pytest.raises(UnicodeDecodeError):
        baseline.load(tmp_path / "bad.json")


def test_baseline_save_writes_sorted_deduplicated_lf_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    assert baseline.save(path, [V, V, Violation("crlf", "z", "m")]) == 2
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"}\n")
    assert json.loads(raw)["violations"] == sorted({V.key, Violation("crlf", "z", "m").key})
    assert baseline.load(path) == {V.key, Violation("crlf", "z", "m").key}


def test_baseline_save_does_not_create_missing_parents(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        baseline.save(tmp_path / "no" / "b.json", [V])


# --- config ----------------------------------------------------------------------------------------------------

TOOL = '[tool.archlint]\nsources = ["app"]\nrings = ["domain"]\n[tool.archlint.ring.domain]\nexternal = []\n[tool.archlint.classify]\n"app" = "domain"\n'


def test_find_policy_prefers_archlint_toml_and_skips_a_pyproject_without_the_table(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(PolicyError):
        find_policy(deep)
    (tmp_path / "a" / "pyproject.toml").write_text(TOOL, encoding="utf-8")
    assert find_policy(deep) == tmp_path / "a" / "pyproject.toml"
    (tmp_path / "archlint.toml").write_text("", encoding="utf-8")  # a nearer folder wins over an outer archlint.toml
    assert find_policy(deep) == tmp_path / "a" / "pyproject.toml"


def test_find_policy_raises_on_invalid_toml_and_non_utf8(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[[[", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        find_policy(tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        find_policy(tmp_path)


def test_load_policy_reads_pyproject_archlint_toml_and_crlf(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes(TOOL.replace("\n", "\r\n").encode())
    policy, root = load_policy(tmp_path / "pyproject.toml")
    assert root == tmp_path and policy.sources == ("app",)
    (tmp_path / "archlint.toml").write_text(TOOL.split("\n", 1)[1].replace("tool.archlint.", "").replace("[tool.archlint]", ""), encoding="utf-8")


def test_load_policy_edge_inputs(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "pyproject.toml")
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "missing.toml")


# --- practices -------------------------------------------------------------------------------------------------


def test_sibling_versions_with_no_match_or_without_a_project_table(tmp_path: Path) -> None:
    assert sibling_versions(tmp_path, "packages/*/pyproject.toml") == []
    write_files(tmp_path, {"packages/a/pyproject.toml": "[tool.x]\n"})
    assert sibling_versions(tmp_path, "packages/*/pyproject.toml") == []


def test_secrets_guard_edge_inputs(tmp_path: Path) -> None:
    assert secrets_guard(tmp_path, "tests") == []  # no tests directory
    (tmp_path / "tests").mkdir()
    assert [v.rule for v in secrets_guard(tmp_path, "tests")] == ["secrets-guard"]  # no conftest
    (tmp_path / "tests" / "conftest.py").write_bytes(b"@pytest.fixture(autouse=True)\r\ndef s(monkeypatch):\r\n    monkeypatch.delenv('K')\r\n")
    assert secrets_guard(tmp_path, "tests") == []  # CRLF source still passes
    (tmp_path / "tests" / "conftest.py").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        secrets_guard(tmp_path, "tests")


def test_docs_sync_edge_inputs(repo: Path) -> None:
    policy = mini_policy()
    known = frozenset({"app", "app.domain"})
    assert [v.rule for v in docs_sync(repo, "docs/a.md", policy, known)] == ["docs-sync"]  # missing file
    write_files(repo, {"docs/a.md": ""})
    assert {v.subject for v in docs_sync(repo, "docs/a.md", policy, known)} == known  # empty file: nothing listed
    (repo / "docs" / "a.md").write_bytes(b"| `app` | domain |\r\n| `app.domain` | domain |\r\n")
    assert docs_sync(repo, "docs/a.md", policy, known) == []  # CRLF table
    (repo / "docs" / "a.md").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        docs_sync(repo, "docs/a.md", policy, known)


def test_crlf_ignores_noise_directories_and_non_matching_names(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.sh").write_bytes(b"a\r\n")
    (tmp_path / "x.txt").write_bytes(b"a\r\n")
    (tmp_path / "ok.sh").write_bytes(b"a\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "bad.sh").write_bytes(b"a\r\nb")
    assert [v.subject for v in crlf(tmp_path, ["*.sh"])] == ["sub/bad.sh"]


def test_undeclared_imports_with_a_missing_or_crlf_manifest(repo: Path) -> None:
    policy = mini_policy()
    write_files(repo, {"app/adapters.py": "import requests\n"})
    scan = FsScanner().scan(policy, repo)
    assert [v.subject for v in undeclared_imports(repo, policy, scan.modules, scan.known, "pyproject.toml")] == ["requests"]
    (repo / "pyproject.toml").write_bytes(b'[project]\r\nname = "x"\r\ndependencies = ["requests"]\r\n')
    assert undeclared_imports(repo, policy, scan.modules, scan.known, "pyproject.toml") == []


# --- drift -----------------------------------------------------------------------------------------------------


def test_drift_reports_missing_differing_and_crlf_files(repo: Path) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    checkers = by_name(["pyright"])
    expected = generate(policy, sorted(scan.known), checkers)
    assert len(drift(policy, repo, sorted(scan.known), checkers)) == len(expected)  # all missing
    for rel, text in expected.items():
        (repo / rel).write_bytes(text.encode())
    assert drift(policy, repo, sorted(scan.known), checkers) == []
    rel = next(iter(expected))
    (repo / rel).write_bytes(expected[rel].replace("\n", "\r\n").encode())  # CRLF is drift: bytes are compared
    assert len(drift(policy, repo, sorted(scan.known), checkers)) == 1


# --- selftest --------------------------------------------------------------------------------------------------


def test_selftest_works_on_a_copy_and_leaves_the_repository_untouched(repo: Path) -> None:
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    before = tree(repo)
    report = selftest(mini_policy(), repo, Services(FsScanner(), FakeToolRunner(), CHECKERS))
    assert report.ok
    assert tree(repo) == before


# --- bench -----------------------------------------------------------------------------------------------------

BENCH_TOML = (
    'sources = ["app"]\nrings = ["domain"]\ntype_checkers = ["pyright"]\n[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n"app.domain" = "domain"\n'
)


def test_bench_writes_the_generated_configs_and_clears_the_cache(tmp_path: Path) -> None:
    write_files(tmp_path, {"app/__init__.py": "", "app/domain.py": "X = 1\n", "archlint.toml": BENCH_TOML})
    (tmp_path / ".archlint").mkdir()
    (tmp_path / ".archlint" / "stale").write_text("x", encoding="utf-8")
    lines = bench_one(tmp_path, ["pyright"], lambda r: Services(FsScanner(r / ".archlint"), FakeToolRunner(), CHECKERS))
    assert lines[0].startswith("### ") and any("archlint own checks" in ln for ln in lines)
    assert (tmp_path / "pyrightconfig.json").is_file() and (tmp_path / "archlint-importlinter.toml").is_file()
    assert not (tmp_path / ".archlint" / "stale").exists()


def test_the_mini_repo_fixture_is_what_these_tests_assume() -> None:
    assert "app/domain.py" in MINI_FILES
