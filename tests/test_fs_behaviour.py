"""Every file access in the application ring, through the FileSystem port: baseline, config, the practices and drift
run on the real disk AND on the in-memory fake (same assertions); selftest and bench need the real scanner and so run on
disk. Written as characterization tests before the port existed (git history), so the edge inputs (missing, empty,
CRLF, non-UTF-8, a directory where a file should be) pin the behaviour the port must keep."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from archlint_helpers import MINI_FILES, mini_policy, write_files
from fs_helpers import KINDS, make_fs, put

from archlint import baseline
from archlint.adapters import FsScanner, LocalFileSystem, SystemClock
from archlint.bench import bench_one
from archlint.config import find_policy, load_policy
from archlint.domain import PolicyError, Violation
from archlint.engine import Services, drift
from archlint.facts import ModuleInfo, extract
from archlint.generate import generate
from archlint.ports import FileSystem
from archlint.practices import (
    crlf,
    docs_sync,
    secrets_guard,
    sibling_versions,
    undeclared_imports,
)
from archlint.selftest import selftest
from archlint.testing import FakeToolRunner
from archlint.typecheckers import all_checkers, by_name

CHECKERS = list(all_checkers().values())
V = Violation("internal-import", "a.b", "msg")
Env = tuple[FileSystem, Path]


@pytest.fixture(params=KINDS)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Env:
    fs, root = make_fs(request.param, tmp_path)
    fs.mkdir(root)
    return fs, root


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --- baseline --------------------------------------------------------------------------------------------------


def test_baseline_load_of_a_missing_file_or_a_directory_is_empty(env: Env) -> None:
    fs, root = env
    assert baseline.load(fs, root / "nope.json") == set()
    fs.mkdir(root / "dir")
    assert baseline.load(fs, root / "dir") == set()


def test_baseline_load_reads_crlf_and_a_missing_violations_key(env: Env) -> None:
    fs, root = env
    put(fs, root, {"b.json": b'{\r\n  "violations": ["x|y"]\r\n}\r\n', "c.json": "{}"})
    assert baseline.load(fs, root / "b.json") == {"x|y"}
    assert baseline.load(fs, root / "c.json") == set()


def test_baseline_load_rejects_empty_and_non_utf8_files(env: Env) -> None:
    fs, root = env
    put(fs, root, {"empty.json": b"", "bad.json": b'{"violations": ["\xff"]}'})
    with pytest.raises(ValueError):
        baseline.load(fs, root / "empty.json")
    with pytest.raises(UnicodeDecodeError):
        baseline.load(fs, root / "bad.json")


def test_baseline_save_writes_sorted_deduplicated_lf_and_round_trips(env: Env) -> None:
    fs, root = env
    other = Violation("crlf", "z", "m")
    assert baseline.save(fs, root / "b.json", [V, V, other]) == 2
    raw = fs.read_bytes(root / "b.json")
    assert b"\r" not in raw and raw.endswith(b"}\n")
    assert json.loads(raw)["violations"] == sorted({V.key, other.key})
    assert baseline.load(fs, root / "b.json") == {V.key, other.key}


def test_baseline_save_does_not_create_missing_parents(env: Env) -> None:
    fs, root = env
    with pytest.raises(FileNotFoundError):
        baseline.save(fs, root / "no" / "b.json", [V])


# --- config ----------------------------------------------------------------------------------------------------

TOOL = '[tool.archlint]\nsources = ["app"]\nrings = ["domain"]\n[tool.archlint.ring.domain]\nexternal = []\n[tool.archlint.classify]\n"app" = "domain"\n'
FLAT = 'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n'


def test_find_policy_prefers_the_nearest_folder_and_skips_a_pyproject_without_the_table(env: Env) -> None:
    fs, root = env
    deep = root / "a" / "b"
    fs.mkdir(deep)
    put(fs, root, {"pyproject.toml": '[project]\nname = "x"\n'})
    with pytest.raises(PolicyError):
        find_policy(fs, deep)
    put(fs, root, {"a/pyproject.toml": TOOL, "archlint.toml": ""})
    assert find_policy(fs, deep) == root / "a" / "pyproject.toml"  # a nearer folder wins over an outer archlint.toml


def test_find_policy_raises_on_invalid_toml_and_non_utf8(env: Env) -> None:
    fs, root = env
    put(fs, root, {"pyproject.toml": "[[["})
    with pytest.raises(tomllib.TOMLDecodeError):
        find_policy(fs, root)
    put(fs, root, {"pyproject.toml": b"\xff\xfe"})
    with pytest.raises(UnicodeDecodeError):
        find_policy(fs, root)


def test_load_policy_reads_pyproject_archlint_toml_and_crlf(env: Env) -> None:
    fs, root = env
    put(fs, root, {"pyproject.toml": TOOL.replace("\n", "\r\n"), "archlint.toml": FLAT})
    policy, found_root = load_policy(fs, root / "pyproject.toml")
    assert found_root == root and policy.sources == ("app",)
    assert load_policy(fs, root / "archlint.toml")[0].sources == ("app",)


def test_load_policy_edge_inputs(env: Env) -> None:
    fs, root = env
    put(fs, root, {"pyproject.toml": "[project]\nname='x'\n"})
    with pytest.raises(PolicyError):
        load_policy(fs, root / "pyproject.toml")
    with pytest.raises(FileNotFoundError):
        load_policy(fs, root / "missing.toml")


# --- practices -------------------------------------------------------------------------------------------------


def test_sibling_versions_with_no_match_or_without_a_project_table(env: Env) -> None:
    fs, root = env
    assert sibling_versions(fs, root, "packages/*/pyproject.toml") == []
    put(fs, root, {"packages/a/pyproject.toml": "[tool.x]\n"})
    assert sibling_versions(fs, root, "packages/*/pyproject.toml") == []


def test_sibling_versions_catches_a_stale_pin(env: Env) -> None:
    fs, root = env
    put(fs, root, {
        "packages/a/pyproject.toml": '[project]\nname = "a"\nversion = "0.2.0"\n',
        "packages/b/pyproject.toml": '[project]\nname = "b"\nversion = "1"\ndependencies = ["a==0.1.1"]\n',
    })  # fmt: skip
    found = sibling_versions(fs, root, "packages/*/pyproject.toml")
    assert [(v.rule, v.subject) for v in found] == [("sibling-versions", "packages/b/pyproject.toml")]


def test_secrets_guard_edge_inputs(env: Env) -> None:
    fs, root = env
    assert secrets_guard(fs, root, "tests") == []  # no tests directory
    fs.mkdir(root / "tests")
    assert [v.rule for v in secrets_guard(fs, root, "tests")] == ["secrets-guard"]  # no conftest
    put(fs, root, {"tests/conftest.py": b"@pytest.fixture(autouse=True)\r\ndef s(monkeypatch):\r\n    monkeypatch.delenv('K')\r\n"})
    assert secrets_guard(fs, root, "tests") == []  # CRLF source still passes
    put(fs, root, {"tests/conftest.py": b"\xff"})
    with pytest.raises(UnicodeDecodeError):
        secrets_guard(fs, root, "tests")


def test_docs_sync_edge_inputs(env: Env) -> None:
    fs, root = env
    policy = mini_policy()
    known = frozenset({"app", "app.domain"})
    assert [v.rule for v in docs_sync(fs, root, "docs/a.md", policy, known)] == ["docs-sync"]  # missing file
    put(fs, root, {"docs/a.md": ""})
    assert {v.subject for v in docs_sync(fs, root, "docs/a.md", policy, known)} == known  # empty file: nothing listed
    put(fs, root, {"docs/a.md": b"| `app` | domain |\r\n| `app.domain` | domain |\r\n"})
    assert docs_sync(fs, root, "docs/a.md", policy, known) == []  # CRLF table
    put(fs, root, {"docs/a.md": b"\xff"})
    with pytest.raises(UnicodeDecodeError):
        docs_sync(fs, root, "docs/a.md", policy, known)


def test_crlf_ignores_noise_directories_and_non_matching_names(env: Env) -> None:
    fs, root = env
    put(fs, root, {"node_modules/x.sh": b"a\r\n", "x.txt": b"a\r\n", "ok.sh": b"a\n", "sub/bad.sh": b"a\r\nb"})
    assert [v.subject for v in crlf(fs, root, ["*.sh"])] == ["sub/bad.sh"]


def test_undeclared_imports_with_a_missing_or_crlf_manifest(env: Env) -> None:
    fs, root = env
    policy = mini_policy()
    src = "import requests\n"
    modules = {"app.adapters": ModuleInfo("app.adapters", "app/adapters.py", False, extract(src, "app/adapters.py"))}
    known = frozenset({"app", "app.adapters"})
    assert [v.subject for v in undeclared_imports(fs, root, policy, modules, known, "pyproject.toml")] == ["requests"]
    put(fs, root, {"pyproject.toml": b'[project]\r\nname = "x"\r\ndependencies = ["requests"]\r\n'})
    assert undeclared_imports(fs, root, policy, modules, known, "pyproject.toml") == []


# --- drift -----------------------------------------------------------------------------------------------------


def test_drift_reports_missing_differing_and_crlf_files(env: Env) -> None:
    fs, root = env
    policy = mini_policy()
    names = ["app", "app.domain", "app.ports", "app.adapters", "app.main"]
    checkers = by_name(["pyright"])
    expected = generate(policy, names, checkers)
    assert drift(fs, policy, root, names, checkers) == []  # optional presets need not exist
    put(fs, root, dict(expected))
    assert drift(fs, policy, root, names, checkers) == []
    rel = next(iter(expected))
    put(fs, root, {rel: expected[rel].replace("\n", "\r\n")})  # CRLF is drift: the content is compared exactly
    assert len(drift(fs, policy, root, names, checkers)) == 1


# --- selftest and bench (they need the real scanner, so the real disk) -----------------------------------------


def test_selftest_works_on_a_copy_and_leaves_the_repository_untouched(repo: Path) -> None:
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    before = tree(repo)
    report = selftest(mini_policy(), repo, Services(FsScanner(), FakeToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), []))
    assert report.ok
    assert tree(repo) == before


BENCH_TOML = 'type_checkers = ["pyright"]\n' + FLAT.replace('"app" = "domain"\n', '"app" = "domain"\n"app.domain" = "domain"\n')


def test_bench_writes_the_generated_configs_and_clears_the_cache(tmp_path: Path) -> None:
    write_files(tmp_path, {"app/__init__.py": "", "app/domain.py": "X = 1\n", "archlint.toml": BENCH_TOML})
    (tmp_path / ".archlint").mkdir()
    (tmp_path / ".archlint" / "stale").write_text("x", encoding="utf-8")
    lines = bench_one(tmp_path, ["pyright"], lambda r: Services(FsScanner(r / ".archlint"), FakeToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), []))
    assert lines[0].startswith("### ") and any("archlint own checks" in ln for ln in lines)
    assert (tmp_path / "pyrightconfig.json").is_file() and (tmp_path / "archlint-importlinter.toml").is_file()
    assert not (tmp_path / ".archlint" / "stale").exists()


def test_the_mini_repo_fixture_is_what_these_tests_assume() -> None:
    assert "app/domain.py" in MINI_FILES
