"""#163 stage 3: ``check --admitted-against REF`` fails a module that is new since REF and was not validly admitted.

The first group pins how existing callers use what this change touches (``Options``, argv forwarding, the ``classify-facts``
inner-ring signal and its exemptions); the second drives the diff check against a scripted ``git``.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent.adapters import FsScanner
from arborescent.admitted_against import admission_problems, new_module_paths
from arborescent.classify_audit import (
    AdmissionOutcome,
    admit_check,
    audit_classification,
    tighter_ring,
)
from arborescent.cli import main
from arborescent.domain import Rule, ToolResult
from arborescent.engine import Options, Services, child_arbor_command
from arborescent.rules import RuleContext

HEADER = "from __future__ import annotations\n\n"
CONFIG = (
    'sources = ["app"]\nrings = ["domain", "ports", "adapters", "composition"]\n'
    '[groups]\npure = ["__future__", "dataclasses", "typing", "collections"]\n'
    '[ring.domain]\nexternal = ["@pure"]\n[ring.ports]\nmay_import = ["domain"]\nexternal = ["@pure"]\n'
    '[ring.adapters]\nmay_import = ["domain", "ports"]\nexternal = ["@pure", "os"]\n'
    '[ring.composition]\nmay_import = ["*"]\nexternal = ["*"]\n'
    '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.ports" = "ports"\n"app.adapters" = "adapters"\n"app.main" = "composition"\n'
)


# -- pins: what existing callers rely on ----------------------------------------------------------------------------------------

def test_options_default_to_no_admission_check() -> None:
    assert Options().admitted_against is None


def test_growth_against_is_still_forwarded_to_child_arbors(tmp_path: Path) -> None:
    argv = child_arbor_command(tmp_path / "arborescent.toml", Options(growth_against="main"))
    assert argv[argv.index("--growth-against") + 1] == "main"
    assert "--admitted-against" not in argv


def test_a_child_arbor_command_forwards_the_admission_ref(tmp_path: Path) -> None:
    argv = child_arbor_command(tmp_path / "arborescent.toml", Options(admitted_against="origin/main"))
    assert argv[argv.index("--admitted-against") + 1] == "origin/main"
    assert "--growth-against" not in argv
    assert "--admitted-against" not in child_arbor_command(tmp_path / "arborescent.toml", Options())


def _inner_ring_notes(root: Path, source: str) -> list[str]:
    write_files(root, {**MINI_FILES, "app/mod.py": source})
    policy = mini_policy()
    scan = FsScanner().scan(policy, root)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    found = audit_classification(context, scan.modules, sorted(scan.known))
    return [v.detail for v in found if v.rule == Rule.CLASSIFY_FACTS and v.subject == "app.mod" and "could sit in ring" in v.detail]


def test_the_inner_ring_note_still_exempts_a_module_with_no_definitions(tmp_path: Path) -> None:
    assert _inner_ring_notes(tmp_path, HEADER + "from . import domain\n") == []


def test_the_inner_ring_note_still_exempts_a_main_guard_and_an_abstract_class(tmp_path: Path) -> None:
    assert _inner_ring_notes(tmp_path, HEADER + 'def f() -> int:\n    return 1\n\nif __name__ == "__main__":\n    f()\n') == []
    abstract = HEADER + "from abc import ABC, abstractmethod\n\nclass A(ABC):\n    @abstractmethod\n    def f(self) -> int: ...\n"
    assert _inner_ring_notes(tmp_path, abstract) == []


def test_the_inner_ring_note_still_fires_for_a_plain_looser_module(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "app/mod.py": HEADER + "def f() -> int:\n    return 1\n"})
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    info = scan.modules["app.mod"]
    assert tighter_ring(context, "app.mod", info, "adapters", sorted(scan.known)) == "domain"


def test_tighter_ring_is_none_for_the_innermost_ring_and_for_a_module_that_needs_its_ring(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "app/mod.py": HEADER + "import os\n\nX = os.sep\n"})
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    known = sorted(scan.known)
    assert tighter_ring(context, "app.mod", scan.modules["app.mod"], "adapters", known) is None
    assert tighter_ring(context, "app.mod", scan.modules["app.mod"], "domain", known) is None


def test_admit_check_still_names_the_tighter_ring_and_leaves_the_outcome_shape_alone(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "app/mod.py": HEADER + "X: int = 1\n"})
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    outcome = admit_check(context, "app.mod", scan.modules["app.mod"], "adapters", sorted(scan.known))
    assert not outcome.admitted
    assert outcome.refusals == ("ring 'domain' also fits; use it, or record why 'adapters' is needed as ring_reason",)
    assert AdmissionOutcome("m", "r", True, (), (), "e").admitted  # positional construction (existing callers) still works


# -- a scripted git -------------------------------------------------------------------------------------------------------------

@dataclass
class Git:
    """Answers `merge-base` and the added-files diff; ``diff`` is the raw ``--name-status`` output. Records every call."""

    diff: str = ""
    base: ToolResult = field(default_factory=lambda: ToolResult(0, "abc123\n", ""))
    diff_result: ToolResult | None = None
    calls: list[list[str]] = field(default_factory=lambda: [])

    def available(self, executable: str) -> bool:
        return executable == "git"

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        self.calls.append(list(argv))
        if "merge-base" in argv:
            return self.base
        if "diff" in argv:
            return self.diff_result or ToolResult(0, self.diff, "")
        return ToolResult(1, "", "unexpected command")


def added(*paths: str) -> str:
    return "".join(f"A\t{p}\n" for p in paths)


def problems(root: Path, git: Git, files: dict[str, str]) -> list[str]:
    write_files(root, {**MINI_FILES, **files})
    policy = mini_policy()
    scan = FsScanner().scan(policy, root)
    return admission_problems("origin/main", policy, root, scan, git)


ANNOTATED_DOMAIN = '__arbor__ = {"ring": "domain"}\n' + HEADER + "X: int = 1\n"


def test_a_validly_admitted_new_module_passes(tmp_path: Path) -> None:
    assert problems(tmp_path, Git(added("app/mod.py")), {"app/mod.py": ANNOTATED_DOMAIN}) == []


def test_new_modules_are_read_through_the_runner_with_rename_detection(tmp_path: Path) -> None:
    git = Git(added("app/mod.py"))
    problems(tmp_path, git, {"app/mod.py": ANNOTATED_DOMAIN})
    merge_base, diff = git.calls
    assert merge_base[-3:] == ["merge-base", "origin/main", "HEAD"] or merge_base[-2:] == ["origin/main", "HEAD"]
    assert "-M" in diff and "--diff-filter=A" in diff and "--name-status" in diff
    assert diff[-2:] == ["abc123", "HEAD"]


def test_a_hand_annotated_new_module_that_violates_its_ring_fails(tmp_path: Path) -> None:
    source = '__arbor__ = {"ring": "domain"}\n' + HEADER + "import os\n\nX = os.sep\n"
    found = problems(tmp_path, Git(added("app/mod.py")), {"app/mod.py": source})
    assert len(found) == 1 and found[0].startswith("admission-refused: app.mod [domain]")


def test_a_new_module_placed_only_by_a_classify_glob_fails(tmp_path: Path) -> None:
    found = problems(tmp_path, Git(added("app/domain2.py")), {"app/domain2.py": HEADER + "X: int = 1\n"})
    assert len(found) == 1 and "no explicit `__arbor__` ring" in found[0]


def test_a_looser_than_needed_new_module_fails_unless_a_reason_is_recorded(tmp_path: Path) -> None:
    loose = '__arbor__ = {"ring": "adapters"}\n' + HEADER + "X: int = 1\n"
    found = problems(tmp_path, Git(added("app/mod.py")), {"app/mod.py": loose})
    assert len(found) == 1 and found[0].startswith("admission-looser-than-needed: app.mod [adapters]")
    assert "domain" in found[0]
    excused = '__arbor__ = {"ring": "adapters", "ring_reason": "wraps os next sprint"}\n' + HEADER + "X: int = 1\n"
    assert problems(tmp_path, Git(added("app/mod.py")), {"app/mod.py": excused}) == []


def test_a_new_module_with_a_malformed_annotation_fails(tmp_path: Path) -> None:
    found = problems(tmp_path, Git(added("app/mod.py")), {"app/mod.py": '__arbor__ = {"ring": 3}\n' + HEADER})
    assert len(found) == 1 and found[0].startswith("admission-refused: app.mod")


def test_the_inner_ring_exemptions_do_not_excuse_a_new_module(tmp_path: Path) -> None:
    """``classify-facts`` skips a module with no definitions, so a bare shim is never noted; admission still refuses it."""
    shim = '__arbor__ = {"ring": "ports"}\n' + HEADER + "from . import domain\n"
    found = problems(tmp_path, Git(added("app/mod.py")), {"app/mod.py": shim})
    assert len(found) == 1 and found[0].startswith("admission-looser-than-needed: app.mod [ports]")


def test_existing_modules_are_untouched(tmp_path: Path) -> None:
    """Only added files are judged: a looser-than-needed module that is not in the diff is not mentioned."""
    loose = '__arbor__ = {"ring": "adapters"}\n' + HEADER + "X: int = 1\n"
    assert problems(tmp_path, Git(added("app/other.py")), {"app/mod.py": loose, "app/other.py": ANNOTATED_DOMAIN}) == []


def test_a_renamed_module_is_not_new(tmp_path: Path) -> None:
    loose = '__arbor__ = {"ring": "adapters"}\n' + HEADER + "X: int = 1\n"
    git = Git("R095\tapp/old.py\tapp/mod.py\n")
    assert problems(tmp_path, git, {"app/mod.py": loose}) == []


def test_a_deleted_module_is_not_new(tmp_path: Path) -> None:
    assert problems(tmp_path, Git("D\tapp/gone.py\n"), {}) == []


def test_added_files_that_are_not_scanned_python_are_ignored(tmp_path: Path) -> None:
    assert problems(tmp_path, Git(added("README.md", "tests/test_x.py", "app/data.json")), {}) == []


def test_an_empty_diff_passes(tmp_path: Path) -> None:
    assert problems(tmp_path, Git(""), {}) == []


def test_windows_separators_in_a_path_still_map_to_the_module(tmp_path: Path) -> None:
    found = problems(tmp_path, Git("A\tapp\\mod.py\n"), {"app/mod.py": '__arbor__ = {"ring": "adapters"}\n' + HEADER + "X: int = 1\n"})
    assert len(found) == 1 and "app.mod" in found[0]


def test_an_unknown_ref_or_shallow_clone_fails_loudly(tmp_path: Path) -> None:
    git = Git(base=ToolResult(1, "", "fatal: Not a valid object name origin/main"))
    with pytest.raises(ValueError, match="merge base"):
        problems(tmp_path, git, {})
    assert not any("diff" in call for call in git.calls)  # nothing is guessed after a failed merge-base


def test_an_empty_merge_base_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="merge base"):
        problems(tmp_path, Git(base=ToolResult(0, "\n", "")), {})


def test_a_failed_diff_fails_loudly_never_passes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not list"):
        problems(tmp_path, Git(diff_result=ToolResult(128, "", "fatal: bad object")), {})


def test_a_ref_that_looks_like_an_option_is_refused_before_git_runs(tmp_path: Path) -> None:
    git = Git()
    with pytest.raises(ValueError, match="not a usable ref"):
        new_module_paths(git, tmp_path, "--output=x")
    with pytest.raises(ValueError, match="not a usable ref"):
        new_module_paths(git, tmp_path, "  ")
    assert git.calls == []


def test_a_second_failure_after_the_merge_base_is_still_loud(tmp_path: Path) -> None:
    git = Git(diff_result=ToolResult(1, "", "boom"))
    with pytest.raises(ValueError, match="boom"):
        new_module_paths(git, tmp_path, "origin/main")


# -- the command ----------------------------------------------------------------------------------------------------------------

def run_check(root: Path, git: Git, *extra: str) -> int:
    (root / "arborescent.toml").write_text(CONFIG, encoding="utf-8")
    args = ["--config", str(root / "arborescent.toml"), "check", "--no-tools", "--no-baseline", *extra]
    base = services()
    return main(args, Services(base.scanner, git, base.checkers, base.fs, base.clock, base.engines, base.packages))


def test_check_admitted_against_passes_and_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/mod.py": ANNOTATED_DOMAIN})
    assert run_check(repo, Git(added("app/mod.py")), "--admitted-against", "origin/main") == 0
    write_files(repo, {"app/mod.py": '__arbor__ = {"ring": "adapters"}\n' + HEADER + "X: int = 1\n"})
    assert run_check(repo, Git(added("app/mod.py")), "--admitted-against", "origin/main") == 1
    assert "admission-looser-than-needed: app.mod" in capsys.readouterr().out


def test_check_admitted_against_fails_on_an_unreadable_ref(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git = Git(base=ToolResult(128, "", "fatal: bad revision"))
    assert run_check(repo, git, "--admitted-against", "nope") == 1
    assert "could not verify admission" in capsys.readouterr().out


def test_check_without_the_flag_never_runs_git_for_admission(repo: Path) -> None:
    git = Git()
    assert run_check(repo, git) == 0
    assert not any("merge-base" in call for call in git.calls)


def test_no_caller_discards_the_admission_diff_result() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "arborescent"
    users: list[str] = []
    for path in src.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            call = node.value if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) else None
            called = getattr(call.func, "id", "") if call is not None else ""
            if called in {"admission_problems", "new_module_paths"}:
                pytest.fail(f"{path.name}:{node.lineno} discards the result of {called}")  # type: ignore[attr-defined]
        if path.name != "admitted_against.py" and "admission_problems" in path.read_text(encoding="utf-8"):
            users.append(path.name)
    assert users == ["cli.py"]  # a new caller must be added to this scan on purpose
