"""``arborescent purity --path/--module`` (#217): only the listed rows change; ``importers`` still counts the whole scan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_mixed_purity import HYDRATION, RINGS

from arborescent.cli import main
from arborescent.cli_purity import Listing, run_purity


def _fixture(repo: Path) -> None:
    """``app.domain`` has two importers, neither of them mixed; ``app.adapters`` has none."""
    write_files(repo, {
        "app/__init__.py": "", "app/adapters.py": HYDRATION, "app/domain.py": HYDRATION,
        "app/ports.py": "from . import domain\n", "app/main.py": "from . import domain\n",
        "notes.txt": "not python\n",
    })


def _run(repo: Path, as_json: bool = True, **listing: object) -> int:
    return run_purity(mini_policy(ring=RINGS), repo, services(), as_json, Listing(**listing))  # type: ignore[arg-type]


def _modules(capsys: pytest.CaptureFixture[str]) -> list[tuple[str, int]]:
    return [(row["module"], row["importers"]) for row in json.loads(capsys.readouterr().out)]


@pytest.mark.parametrize("listing", [
    {"modules": ("app.domain",)},
    {"modules": ("app.dom*",)},
    {"paths": ("app/domain.py",)},
    {"paths": ("./app/../app/domain.py",)},
    {"modules": ("app.domain",), "paths": ("app/domain.py",)},
])
def test_a_scoped_row_keeps_the_fan_in_of_the_full_graph(repo: Path, capsys: pytest.CaptureFixture[str], listing: dict[str, object]) -> None:
    _fixture(repo)
    assert _run(repo, **listing) == 0
    assert _modules(capsys) == [("app.domain", 2)]


@pytest.mark.parametrize("listing", [
    {"modules": ("app",)},
    {"paths": ("app",)},
    {"paths": (".",)},
    {"paths": ("app/domain.py", "app/adapters.py")},
    {"modules": ("app.adapters",), "paths": ("app/domain.py",)},
])
def test_packages_folders_and_several_selections_are_a_union(repo: Path, capsys: pytest.CaptureFixture[str], listing: dict[str, object]) -> None:
    _fixture(repo)
    assert _run(repo, **listing) == 0
    assert _modules(capsys) == [("app.domain", 2), ("app.adapters", 0)]


def test_relative_paths_start_at_the_working_directory_and_absolute_ones_work(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    assert _run(repo, paths=("domain.py",), cwd=repo / "app") == 0
    assert _modules(capsys) == [("app.domain", 2)]
    assert _run(repo, paths=(str(repo / "app" / "adapters.py"),), cwd=repo / "app") == 0
    assert _modules(capsys) == [("app.adapters", 0)]


def test_a_module_prefix_is_not_a_string_prefix(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    assert _run(repo, modules=("app.dom",)) == 2
    assert "app.dom" in capsys.readouterr().err


@pytest.mark.parametrize("listing", [
    {"paths": ("app/missing.py",)},
    {"paths": ("",)},
    {"paths": ("   ",)},
    {"paths": ("app/\0.py",)},
    {"paths": ("..",)},
    {"modules": ("",)},
    {"modules": ("nope",)},
    {"modules": ("app\0",)},
    {"paths": ("app/domain.py", "app/missing.py")},
    {"paths": ("x" * 5000,)},
])
def test_a_selection_that_names_nothing_fails_instead_of_reporting_clean(
    repo: Path, capsys: pytest.CaptureFixture[str], listing: dict[str, object],
) -> None:
    _fixture(repo)
    assert _run(repo, **listing) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err


def test_an_existing_file_outside_the_scan_is_noted_and_skipped(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A changed-files list holds files that are not scanned (docs, excluded tests): they must not fail the run."""
    _fixture(repo)
    assert _run(repo, paths=("notes.txt", "app/domain.py")) == 0
    captured = capsys.readouterr()
    assert [row["module"] for row in json.loads(captured.out)] == ["app.domain"]
    assert "notes.txt" in captured.err
    assert _run(repo, paths=("notes.txt",)) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_top_applies_inside_the_scope_and_the_text_says_it_is_scoped(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    assert _run(repo, as_json=False, top=1, modules=("app",)) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "showing 1 of 2 mixed module(s) in the selected scope"
    assert _run(repo, as_json=False, modules=("app.adapters",)) == 0
    out = capsys.readouterr().out
    assert "app.domain" not in out and out.splitlines()[-1] == "1 mixed module(s) in the selected scope"
    assert _run(repo, as_json=False, modules=("app.ports",)) == 0
    assert capsys.readouterr().out.strip() == "no mixed modules in the selected scope"


def test_the_command_line_takes_repeated_and_multi_value_flags(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture(repo)
    write_files(repo, {"arborescent.toml": 'sources = ["app"]\nrings = ["domain"]\n[classify]\n"app" = "domain"\n"app.*" = "domain"\n'})
    monkeypatch.chdir(repo / "app")
    assert main(["purity", "--json", "--path", "domain.py", "--path", "adapters.py"], services()) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["purity", "--json", "--module", "app.domain", "app.adapters"], services()) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


@pytest.mark.parametrize("flags", [["--path", "x.py"], ["--module", "x"]])
def test_explain_does_not_combine_with_a_scope(flags: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["purity", "--explain", "app.domain", *flags])
    assert caught.value.code == 2
    assert "--explain" in capsys.readouterr().err
