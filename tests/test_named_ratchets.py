"""#147: named ratchets: `[ratchet.<name>]` config, the four measures, one baseline format, shrink-only compare, `--growth-against`."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, write_files
from test_baseline_ratchet import write_policy

from arborescent import ratchets
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.baseline import BaselineError
from arborescent.cli import main
from arborescent.domain import PolicyError, RatchetSpec, Rule, ToolResult, Violation
from arborescent.engine import Services
from arborescent.engines import default_engines
from arborescent.ratchet_config import ratchet_specs
from arborescent.ratchet_measure import MeasureError, Subject, measure
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers

BASELINE = "arborescent-ratchets.json"
FS = LocalFileSystem()


@dataclass
class GitRunner:
    """Answers `git ls-tree` / `git show` from a map of path -> text at the ref; anything else is a failed command."""

    at_ref: Mapping[str, str] = field(default_factory=lambda: {})
    ref_ok: bool = True

    def available(self, executable: str) -> bool:
        return executable == "git"

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        if not self.ref_ok:
            return ToolResult(128, "", "fatal: bad revision")
        if argv[:2] == ["git", "ls-tree"]:
            return ToolResult(0, argv[-1] + "\n" if argv[-1] in self.at_ref else "", "")
        if argv[:2] == ["git", "show"] and argv[2].endswith("arborescent-baseline.json"):  # the main baseline's own growth check
            return ToolResult(0, '{"version": 2, "violations": [], "growth": []}', "")
        if argv[:2] == ["git", "show"] and argv[2].split(":./", 1)[-1] in self.at_ref:
            return ToolResult(0, self.at_ref[argv[2].split(":./", 1)[-1]], "")
        return ToolResult(1, "", "unexpected command")


def svc(runner: object | None = None) -> Services:
    return Services(
        FsScanner(), runner or FakeToolRunner(), list(all_checkers().values()), FS, SystemClock(), default_engines()  # pyright: ignore[reportArgumentType]
    )


def project(repo: Path, ratchet_toml: str, main_source: str = "print(1)\nprint(2)\n") -> Path:
    config = write_policy(repo)
    config.write_text(config.read_text() + ratchet_toml, encoding="utf-8")
    write_files(repo, {"app/main.py": main_source})
    return config


PRINTS = '[ratchet.prints]\nast_call = "print"\n'


def run(config: Path, *args: str, services: Services | None = None) -> int:
    return main(["--config", str(config), *args], services or svc())


def check(config: Path, *args: str, services: Services | None = None) -> int:
    return run(config, "check", "--no-tools", "--no-baseline", *args, services=services)


def doc(repo: Path) -> dict[str, Any]:
    return json.loads((repo / BASELINE).read_text(encoding="utf-8"))


def subject(root: Path, paths: Mapping[str, str], found: Sequence[Violation] = (), runner: object | None = None) -> Subject:
    return Subject(root, FS, runner or FakeToolRunner(), paths, found)  # pyright: ignore[reportArgumentType]


# --- configuration ---------------------------------------------------------------------------------------------------


def test_each_measure_is_read_from_its_table() -> None:
    specs = ratchet_specs({
        "r": {"rule": "internal-import", "scope": ["app.*"]},
        "g": {"regex": r"TODO\b", "baseline": "x.json"},
        "c": {"ast_call": "json5.load"},
        "k": {"command": ["python", "scan.py"]},
    })
    assert [(s.name, s.measure, s.argument, s.scope, s.baseline) for s in specs] == [
        ("c", "ast_call", ("json5.load",), (), BASELINE),
        ("g", "regex", (r"TODO\b",), (), "x.json"),
        ("k", "command", ("python", "scan.py"), (), BASELINE),
        ("r", "rule", ("internal-import",), ("app.*",), BASELINE),
    ]


@pytest.mark.parametrize(
    "table",
    [
        {},
        {"rule": "internal-import", "regex": "x"},
        {"rule": "no-such-rule"},
        {"regex": "("},
        {"regex": ""},
        {"command": []},
        {"command": "python scan.py"},
        {"command": ["python", ""]},
        {"ast_call": "print", "scope": "app"},
        {"ast_call": "print", "scope": [""]},
        {"ast_call": "print", "baseline": " "},
        {"ast_call": "print", "mode": "loose"},
        "print",
    ],
)
def test_a_malformed_ratchet_table_is_refused(table: object) -> None:
    with pytest.raises(PolicyError):
        ratchet_specs({"n": table})


@pytest.mark.parametrize("name", ["", "a|b", "-a", "a b"])
def test_a_ratchet_name_cannot_break_the_growth_key(name: str) -> None:
    with pytest.raises(PolicyError):
        ratchet_specs({name: {"ast_call": "print"}})


def test_ratchets_must_be_a_table_and_default_to_none() -> None:
    assert mini_policy().ratchets == ()
    with pytest.raises(PolicyError):
        ratchet_specs(["x"])


# --- measures --------------------------------------------------------------------------------------------------------


def write_modules(root: Path, sources: Mapping[str, str]) -> dict[str, str]:
    write_files(root, {f"{m.replace('.', '/')}.py": text for m, text in sources.items()})
    return {m: f"{m.replace('.', '/')}.py" for m in sources}


def spec(measure_name: str, *argument: str, scope: tuple[str, ...] = ()) -> RatchetSpec:
    return RatchetSpec("n", measure_name, argument, scope)


def test_ast_call_counts_dotted_calls_not_names_strings_or_comments(tmp_path: Path) -> None:
    paths = write_modules(tmp_path, {
        "a": "import json5\nprint(1)\nx = print\n# print(2)\ns = 'print(3)'\njson5.load(f)\nobj.json5.load(g)\n",
        "b": "def f():\n    print(1)\n    print(2)\n",
        "c": "x = 1\n",
    })
    assert measure(spec("ast_call", "print"), subject(tmp_path, paths)) == {"a": 1, "b": 2}
    assert measure(spec("ast_call", "json5.load"), subject(tmp_path, paths)) == {"a": 1}


def test_regex_counts_matches_per_line_start_and_skips_zero(tmp_path: Path) -> None:
    paths = write_modules(tmp_path, {"a": "    import os\nimport re\n", "b": "x = 1\n"})
    assert measure(spec("regex", r"^\s+import "), subject(tmp_path, paths)) == {"a": 1}


def test_scope_limits_which_modules_count(tmp_path: Path) -> None:
    paths = write_modules(tmp_path, {"a": "print(1)\n", "b": "print(1)\n"})
    assert measure(spec("ast_call", "print", scope=("b",)), subject(tmp_path, paths)) == {"b": 1}


def test_rule_counts_findings_by_subject() -> None:
    found = [Violation(Rule.SUPPRESSION, "a", "x#1"), Violation(Rule.SUPPRESSION, "a", "x#2"), Violation(Rule.DISCARD, "a", "c#1"), Violation(Rule.SUPPRESSION, "b", "x#1")]
    assert measure(spec("rule", "suppression"), subject(Path(), {}, found)) == {"a": 2, "b": 1}
    assert measure(spec("rule", "suppression", scope=("b",)), subject(Path(), {}, found)) == {"b": 1}


@pytest.mark.parametrize("source", ["def (:\n", "x = '\x00'\n"])
def test_a_file_that_cannot_be_parsed_fails_the_measure_instead_of_counting_zero(tmp_path: Path, source: str) -> None:
    paths = write_modules(tmp_path, {"a": source})
    with pytest.raises(MeasureError):
        measure(spec("ast_call", "print"), subject(tmp_path, paths))


def test_an_unreadable_file_fails_the_measure(tmp_path: Path) -> None:
    with pytest.raises(MeasureError):
        measure(spec("regex", "x"), subject(tmp_path, {"gone": "gone.py"}))


def command(stdout: str, code: int | None = 0, stderr: str = "", installed: bool = True) -> Subject:
    runner = FakeToolRunner({"scan"} if installed else set(), {"scan": ToolResult(code, stdout, stderr)})
    return subject(Path(), {}, runner=runner)


def test_a_command_prints_module_count_lines() -> None:
    out = measure(spec("command", "scan"), command("pkg.a 3\n\npkg.b 1\nzero.mod 0\n  my mod   2  \n"))
    assert out == {"my mod": 2, "pkg.a": 3, "pkg.b": 1}
    assert measure(spec("command", "scan"), command("")) == {}


@pytest.mark.parametrize(
    "stdout,code,installed",
    [("a 1\n", 1, True), ("a 1\n", None, True), ("a 1\n", 0, False), ("a\n", 0, True), ("a -1\n", 0, True), ("a x\n", 0, True), ("a 1\na 2\n", 0, True), (" 3\n", 0, True)],
)
def test_a_command_that_fails_or_prints_garbage_fails_the_measure(stdout: str, code: int | None, installed: bool) -> None:
    with pytest.raises(MeasureError):
        measure(spec("command", "scan"), command(stdout, code, "boom", installed))


# --- check, record, shrink-only --------------------------------------------------------------------------------------


def test_check_needs_a_recorded_baseline_then_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(repo, PRINTS)
    assert check(config) == 1
    assert "no baseline" in capsys.readouterr().out
    assert run(config, "ratchet") == 0
    assert doc(repo)["ratchets"] == {"prints": {"app.main": 2}}
    assert check(config) == 0


def test_the_first_recording_logs_itself_as_an_adoption(repo: Path) -> None:
    config = project(repo, PRINTS)
    assert run(config, "ratchet") == 0
    assert doc(repo)["growth"] == [
        {"key": "prints|app.main", "reason": "adopt ratchet prints", "approved-by": "arborescent ratchet", "from": 0, "to": 2}
    ]


def test_a_rise_fails_check_and_cannot_be_recorded_without_attribution(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(repo, PRINTS)
    assert run(config, "ratchet") == 0
    before = (repo / BASELINE).read_text(encoding="utf-8")
    write_files(repo, {"app/main.py": "print(1)\nprint(2)\nprint(3)\n"})
    assert check(config) == 1
    assert "app.main: 3 > baseline 2" in capsys.readouterr().out
    assert run(config, "ratchet") == 2
    for bad in (["--allow-growth"], ["--allow-growth", "--reason", "r"], ["--allow-growth", "--reason", " ", "--approved-by", "me"], ["--reason", "r", "--approved-by", "me"]):
        assert run(config, "ratchet", *bad) == 2
    assert (repo / BASELINE).read_text(encoding="utf-8") == before
    assert run(config, "ratchet", "--allow-growth", "--reason", "new CLI", "--approved-by", "#9") == 0
    assert doc(repo)["ratchets"] == {"prints": {"app.main": 3}}
    assert doc(repo)["growth"][-1] == {"key": "prints|app.main", "reason": "new CLI", "approved-by": "#9", "from": 2, "to": 3}
    assert check(config) == 0


def test_a_new_module_over_zero_is_a_rise(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(repo, PRINTS)
    assert run(config, "ratchet") == 0
    write_files(repo, {"app/domain.py": "print(1)\n"})
    assert check(config) == 1
    assert "app.domain: 1 > baseline 0" in capsys.readouterr().out


def test_a_fall_is_stale_until_the_baseline_is_lowered(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(repo, PRINTS)
    assert run(config, "ratchet") == 0
    write_files(repo, {"app/main.py": "print(1)\n"})
    assert check(config) == 1
    assert "app.main: 1 < baseline 2" in capsys.readouterr().out
    assert run(config, "ratchet") == 0  # lowering needs no flag and logs nothing
    assert doc(repo)["ratchets"] == {"prints": {"app.main": 1}}
    assert len(doc(repo)["growth"]) == 1
    write_files(repo, {"app/main.py": "x = 1\n"})
    assert run(config, "ratchet") == 0
    assert doc(repo)["ratchets"] == {"prints": {}}  # recorded at zero, not forgotten
    assert check(config) == 0


def test_a_partial_run_reports_a_rise_but_never_a_fall(repo: Path) -> None:
    policy = replace(mini_policy(), ratchets=(spec("rule", "suppression"),))
    write_files(repo, {BASELINE: baseline_text({"n": {"a": 2}})})

    def sees(count: int) -> Subject:
        return subject(repo, {}, [Violation(Rule.SUPPRESSION, "a", f"x#{i}") for i in range(count)])
    assert ratchets.check(policy, sees(1), complete=False)[0].failures == []
    assert ratchets.check(policy, sees(3), complete=False)[0].failures
    assert ratchets.check(policy, sees(1), complete=True)[0].failures


def test_one_file_holds_several_ratchets_and_a_name_can_be_recorded_alone(repo: Path) -> None:
    config = project(repo, PRINTS + '[ratchet.imports]\nregex = "^import "\nscope = ["app.main"]\n', "import os\nprint(1)\n")
    assert run(config, "ratchet", "--name", "prints") == 0
    assert doc(repo)["ratchets"] == {"prints": {"app.main": 1}}
    assert run(config, "ratchet") == 0
    assert doc(repo)["ratchets"] == {"imports": {"app.main": 1}, "prints": {"app.main": 1}}
    assert run(config, "ratchet", "--name", "nope") == 2


def test_ratchet_without_config_is_an_error(repo: Path) -> None:
    assert run(write_policy(repo), "ratchet") == 2


def test_a_refused_rise_in_one_ratchet_writes_nothing_for_the_others(repo: Path) -> None:
    config = project(repo, PRINTS + '[ratchet.imports]\nregex = "^import "\nscope = ["app.main"]\n', "import os\nprint(1)\n")
    assert run(config, "ratchet") == 0
    before = (repo / BASELINE).read_text(encoding="utf-8")
    write_files(repo, {"app/main.py": "import os\nimport re\nprint(1)\n"})
    assert run(config, "ratchet") == 2
    assert (repo / BASELINE).read_text(encoding="utf-8") == before


def test_a_measure_that_cannot_run_fails_check_and_ratchet(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(repo, '[ratchet.ext]\ncommand = ["scan"]\n')
    assert check(config) == 1
    assert "scan is not installed" in capsys.readouterr().out
    assert run(config, "ratchet") == 2
    assert not (repo / BASELINE).exists()


def test_a_command_ratchet_adopts_an_existing_scanners_output(repo: Path) -> None:
    config = project(repo, '[ratchet.ext]\ncommand = ["scan"]\nscope = ["app.*"]\n')
    runner = FakeToolRunner({"scan"}, {"scan": ToolResult(0, "app.main 4\nother.mod 9\n", "")})
    assert run(config, "ratchet", services=svc(runner)) == 0
    assert doc(repo)["ratchets"] == {"ext": {"app.main": 4}}
    assert check(config, services=svc(runner)) == 0


@pytest.mark.parametrize("text", ["[]", '{"version": 2}', '{"ratchets": []}', '{"ratchets": {"a": {"m": 0}}}', '{"ratchets": {"a": {"m": true}}}', '{"ratchets": {"a": {"m": 1.5}}}', '{"growth": {}}'])
def test_a_malformed_baseline_file_fails_check_instead_of_reading_as_empty(repo: Path, text: str) -> None:
    config = project(repo, PRINTS)
    (repo / BASELINE).write_text(text, encoding="utf-8")
    assert check(config) == 1
    assert run(config, "ratchet") == 2


# --- growth since a ref ----------------------------------------------------------------------------------------------


def baseline_text(counts: Mapping[str, Mapping[str, int]], growth: Sequence[object] = ()) -> str:
    return json.dumps({"version": 1, "ratchets": counts, "growth": list(growth)})


def entry(key: str, reason: str = "r", approver: str = "me") -> dict[str, object]:
    return {"key": key, "reason": reason, "approved-by": approver}


def growth_problems(repo: Path, current: str, at_ref: Mapping[str, str], ref_ok: bool = True) -> list[str]:
    write_files(repo, {BASELINE: current})
    policy = replace(mini_policy(), ratchets=(spec("regex", "x"),))
    return ratchets.growth_against("base", policy, repo, FS, GitRunner(at_ref, ref_ok))


def test_growth_needs_an_attributed_entry_for_each_added_or_raised_count(repo: Path) -> None:
    old = baseline_text({"n": {"a": 2, "b": 1}}, [entry("n|b")])
    at_ref = {BASELINE: old}
    assert growth_problems(repo, old, at_ref) == []
    assert growth_problems(repo, baseline_text({"n": {"a": 1}}, [entry("n|b")]), at_ref) == []  # a fall needs nothing
    assert growth_problems(repo, baseline_text({"n": {"a": 3, "b": 1}}, [entry("n|b")]), at_ref)  # silent raise
    assert growth_problems(repo, baseline_text({"n": {"a": 2, "b": 1, "c": 1}}, [entry("n|b")]), at_ref)  # silent new module
    assert growth_problems(repo, baseline_text({"n": {"a": 2, "b": 1}, "m": {"a": 1}}, [entry("n|b")]), at_ref)  # silent new ratchet
    raised = {"n": {"a": 3, "b": 1}}
    assert growth_problems(repo, baseline_text(raised, [entry("n|b"), entry("n|a")]), at_ref) == []


@pytest.mark.parametrize("bad", [entry("n|a", approver=""), entry("n|a", reason=" "), entry("n|other"), {"reason": "r", "approved-by": "me"}, "n|a"])
def test_an_unattributed_or_misaddressed_growth_entry_does_not_count(repo: Path, bad: object) -> None:
    at_ref = {BASELINE: baseline_text({"n": {"a": 1}})}
    assert growth_problems(repo, baseline_text({"n": {"a": 2}}, [bad]), at_ref)


def test_rewriting_or_dropping_old_growth_entries_is_caught(repo: Path) -> None:
    at_ref = {BASELINE: baseline_text({"n": {"a": 1}}, [entry("n|a")])}
    assert growth_problems(repo, baseline_text({"n": {"a": 1}}, []), at_ref)
    assert growth_problems(repo, baseline_text({"n": {"a": 1}}, [entry("n|a", reason="edited")]), at_ref)


def test_a_baseline_new_since_the_ref_must_log_every_count(repo: Path) -> None:
    assert growth_problems(repo, baseline_text({"n": {"a": 1}}), {})
    assert growth_problems(repo, baseline_text({"n": {"a": 1}}, [entry("n|a")]), {}) == []
    assert growth_problems(repo, baseline_text({}), {}) == []


def test_an_unreadable_ref_or_baseline_is_a_problem_not_a_pass(repo: Path) -> None:
    ok = baseline_text({"n": {"a": 1}}, [entry("n|a")])
    assert growth_problems(repo, ok, {BASELINE: ok}, ref_ok=False)
    assert growth_problems(repo, "{", {BASELINE: ok})
    assert growth_problems(repo, ok, {BASELINE: "{"})
    assert growth_problems(repo, ok, {BASELINE: "[]"})


def test_check_growth_against_covers_the_ratchet_baselines(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(repo, PRINTS)
    services = svc(GitRunner({BASELINE: baseline_text({"prints": {"app.main": 1}})}))
    write_files(repo, {BASELINE: baseline_text({"prints": {"app.main": 2}})})
    assert check(config, "--growth-against", "base", services=services) == 1
    assert "prints|app.main" in capsys.readouterr().out
    write_files(repo, {BASELINE: baseline_text({"prints": {"app.main": 2}}, [entry("prints|app.main")])})
    assert check(config, "--growth-against", "base", services=services) == 0


def test_a_recorded_rise_passes_growth_against_the_old_baseline(repo: Path) -> None:
    config = project(repo, PRINTS)
    assert run(config, "ratchet") == 0
    old = (repo / BASELINE).read_text(encoding="utf-8")
    write_files(repo, {"app/main.py": "print(1)\nprint(2)\nprint(3)\n"})
    services = svc(GitRunner({BASELINE: old}))
    assert run(config, "ratchet", "--allow-growth", "--reason", "r", "--approved-by", "me") == 0
    assert check(config, "--growth-against", "base", services=services) == 0
    (repo / BASELINE).write_text(old.replace(": 2", ": 3"), encoding="utf-8")  # hand-edited number, no entry
    assert check(config, "--growth-against", "base", services=services) == 1


def test_baseline_error_is_the_refusal_type() -> None:
    with pytest.raises(BaselineError):
        ratchets.record([spec("regex", "x")], subject(Path(), {}), allow_growth=True)


# --- every caller consumes the result --------------------------------------------------------------------------------


def test_no_caller_discards_what_the_ratchet_functions_return() -> None:
    owners = {"ratchets_mod", "named_ratchets", "ratchets"}
    watched = {"check", "growth_against", "record"}
    offenders: list[str] = []
    for path in sorted(Path(__file__).resolve().parents[1].joinpath("src", "arborescent").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            via_module = isinstance(func, ast.Attribute) and func.attr in watched and ast.unparse(func.value) in owners
            if via_module or getattr(func, "id", "") in {"run_ratchet", "growth_run"}:
                offenders.append(f"{path.name}:{node.lineno}: {ast.unparse(node)}")
    assert offenders == []
