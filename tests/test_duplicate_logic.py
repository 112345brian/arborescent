"""``duplicate-logic`` (#228): near-identical function bodies, copies of reference-path code and repeated branch skeletons."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, services, write_files

from arborescent import baseline as ratchet
from arborescent.check_runner import run_check
from arborescent.cli import main
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.duplicate_facts import FunctionShape, function_shapes
from arborescent.engine import Options, blocking, run_own
from arborescent.selftest import plan, selftest

BODY = """\
    cleaned = {arg}.strip().lower()
    parts = cleaned.split("-")
    if not parts:
        return ""
    head = parts[0]
    return head.title()
"""


def func(name: str, arg: str = "raw", doc: str = "") -> str:
    docstring = f'    """{doc}"""\n' if doc else ""
    return f"def {name}({arg}: str) -> str:\n{docstring}{BODY.format(arg=arg)}"


def run(root: Path, files: dict[str, str], table: dict[str, Any] | None = None, cache: Path | None = None) -> list[Violation]:
    write_files(root, files)
    policy = mini_policy(duplicate_logic={} if table is None else table)
    found, _ = run_own(policy, root, services(cache=cache), practices=False)
    return [v for v in found if v.rule == Rule.DUPLICATE_LOGIC]


def shape(source: str) -> FunctionShape:
    (only,) = function_shapes(source)
    return only


# --- normalisation ----------------------------------------------------------------------------------------------------


def test_renamed_parameters_locals_docstrings_comments_and_annotations_do_not_change_the_digest() -> None:
    one = shape('def a(x):\n    """Doc."""\n    y: int = x + 1  # note\n    z = [i for i in y]\n    return z\n')
    two = shape("def b(value):\n    total = value + 1\n    out = [item for item in total]\n    return out\n")
    assert one.digest == two.digest and one.statements == two.statements == 3


@pytest.mark.parametrize(
    "other",
    [
        "def f(x):\n    y = x + 2\n    return str(y)\n",  # a literal
        "def f(x):\n    y = x + 1\n    return repr(y)\n",  # a called name
        "def f(x):\n    y = x.a + 1\n    return str(y)\n",  # an attribute
        "def f(x):\n    y = x - 1\n    return str(y)\n",  # an operator
        "async def f(x):\n    y = x + 1\n    return str(y)\n",
        "def f(x):\n    y = x + True\n    return str(y)\n",  # 1 and True are different literals
        "def f(x):\n    y = x + 1.0\n    return str(y)\n",
    ],
)
def test_literals_called_names_and_structure_are_kept(other: str) -> None:
    assert shape("def f(x):\n    y = x + 1\n    return str(y)\n").digest != shape(other).digest


def test_free_names_and_globals_are_not_renamed() -> None:
    assert shape("def f():\n    return alpha(1)\n").digest != shape("def f():\n    return beta(1)\n").digest
    assert shape("def f():\n    global A\n    A = 1\n").digest != shape("def f():\n    global B\n    B = 1\n").digest


def test_a_recursive_function_matches_its_renamed_copy_but_a_delegating_method_does_not_match_another() -> None:
    assert shape("def fact(n):\n    return n * fact(n - 1)\n").digest == shape("def other(k):\n    return k * other(k - 1)\n").digest
    methods = function_shapes("class C:\n    def exists(self, p):\n        return exists(p)\n    def isfile(self, p):\n        return isfile(p)\n")
    assert methods[0].digest != methods[1].digest


def test_functions_are_listed_with_class_qualified_names_and_repeats_are_numbered() -> None:
    source = (
        "class A:\n    class B:\n        def m(self):\n            return 1\n"
        "if X:\n    def f():\n        return 1\nelse:\n    def f():\n        return 2\n"
        "try:\n    pass\nexcept E:\n    def g():\n        def inner():\n            return 3\n        return inner\n"
    )
    assert [s.qualname for s in function_shapes(source)] == ["A.B.m", "f", "f#2", "g"]  # nested functions belong to their parent


@pytest.mark.parametrize(
    "body",
    [
        "    ...\n",
        "    pass\n",
        '    """Only a docstring."""\n',
        "    raise NotImplementedError\n",
        '    raise NotImplementedError("later")\n',
        "    return NotImplemented\n",
        '    """Doc."""\n    ...\n',
    ],
)
def test_stubs_have_no_statements_to_compare(body: str) -> None:
    assert shape(f"def f(self, a, b):\n{body}").statements == 0


def test_a_protocol_full_of_stubs_is_never_reported_even_at_the_lowest_threshold(repo: Path) -> None:
    protocol = (
        "from __future__ import annotations\nfrom typing import Protocol\n\n\nclass Port(Protocol):\n"
        "    def one(self) -> int: ...\n    def two(self) -> int: ...\n    def three(self) -> int:\n        raise NotImplementedError\n"
    )
    assert run(repo, {"app/ports.py": protocol}, {"min_statements": 1}) == []


def test_statements_are_counted_recursively_without_docstrings_or_bare_annotations() -> None:
    source = 'def f(a):\n    """Doc."""\n    x: int\n    for i in a:\n        if i:\n            a = i\n    return a\n'
    assert shape(source).statements == 4  # for, if, assign, return


@pytest.mark.parametrize("source", ["", "\n", "x = 1\n", "class A:\n    pass\n"])
def test_sources_without_functions_have_no_shapes(source: str) -> None:
    assert function_shapes(source) == ()


def test_odd_literals_do_not_break_hashing() -> None:
    source = f"def f():\n    return (0x{'f' * 6000}, '\\x00', '\\udcff', b'\\x00', 1e999, ..., None, 2j)\n"
    assert shape(source).digest


def test_worst_case_sizes_are_handled_without_recursion_or_quadratic_time() -> None:
    chain = "def f(x):\n    if x == 0:\n        a()\n" + "".join(f"    elif x == {i}:\n        a()\n" for i in range(1, 2500)) + "    else:\n        b()\n"
    long_sum = "def g(x):\n    return " + " + ".join(["x"] * 900) + "\n"  # 3.11 cannot even parse a few thousand terms
    many = "".join(f"def h{i}(x):\n    y = x\n    y += {i}\n    y += 1\n    y += 2\n    return y\n" for i in range(3000))
    start = time.perf_counter()
    shapes = [*function_shapes(chain), *function_shapes(long_sum), *function_shapes(many)]
    assert len(shapes) == 3002
    assert time.perf_counter() - start < 20  # a quadratic walk of the chain would take far longer than this generous bound


# --- the rule ---------------------------------------------------------------------------------------------------------


def test_the_rule_is_off_unless_the_table_is_present(repo: Path) -> None:
    write_files(repo, {"app/domain.py": func("a") + "\n\n" + func("b")})
    for policy in (mini_policy(), mini_policy(duplicate_logic={"enabled": False})):
        found, _ = run_own(policy, repo, services(), practices=False)
        assert not [v for v in found if v.rule == Rule.DUPLICATE_LOGIC]
    assert mini_policy(duplicate_logic={}).duplicate_logic.enabled


def test_duplicates_in_one_module_and_across_modules_are_each_reported_with_their_partners(repo: Path) -> None:
    found = run(repo, {"app/domain.py": func("first") + "\n\n" + func("second", "text", "Same."), "app/ports.py": func("third", "s")})
    assert [(v.subject, v.detail) for v in found] == [
        ("app.domain", "function `first` duplicates logic defined elsewhere"),
        ("app.domain", "function `second` duplicates logic defined elsewhere"),
        ("app.ports", "function `third` duplicates logic defined elsewhere"),
    ]
    assert "app.domain:second" in found[0].hint and "app.ports:third" in found[0].hint
    assert found[0].line == 1 and found[1].line > 1
    assert blocking(found) == found  # fails a check and enters the baseline


def test_a_large_group_lists_a_few_partners_and_counts_the_rest(repo: Path) -> None:
    found = run(repo, {"app/domain.py": "\n\n".join(func(f"f{i}") for i in range(9))})
    assert len(found) == 9 and "and 5 more" in found[0].hint


def test_functions_below_min_statements_are_ignored(repo: Path) -> None:
    small = "def {0}(x):\n    y = x + 1\n    z = y * 2\n    return z\n"
    files = {"app/domain.py": small.format("a") + small.format("b")}
    assert run(repo, files) == []  # default 5
    assert len(run(repo, files, {"min_statements": 3})) == 2


def test_empty_modules_and_a_single_function_report_nothing(repo: Path) -> None:
    assert run(repo, {"app/domain.py": ""}) == []
    assert run(repo, {"app/domain.py": func("a")}) == []


def test_the_key_survives_line_moves_renames_of_partners_and_edits_made_to_both_copies(repo: Path) -> None:
    before = run(repo, {"app/domain.py": func("a"), "app/ports.py": func("b")})
    edited = func("a").replace("title", "upper"), func("renamed").replace("title", "upper")
    after = run(repo, {"app/domain.py": "X = 1\n\n\n" + edited[0], "app/ports.py": edited[1]})
    assert before[0].key == after[0].key


# --- reference paths --------------------------------------------------------------------------------------------------

REFERENCE = {"reference_paths": ["vendor"]}


def test_a_first_party_copy_of_a_vendored_helper_is_reported_against_it_and_vendor_code_is_never_flagged(repo: Path) -> None:
    files = {"vendor/evalkit/text.py": func("normalise") + "\n\n" + func("normalise_again"), "app/domain.py": func("mine", "v")}
    found = run(repo, files, REFERENCE)
    assert [(v.subject, v.detail) for v in found] == [("app.domain", "function `mine` duplicates logic defined elsewhere")]
    assert "vendor/evalkit/text.py:1 normalise" in found[0].hint


def test_a_single_reference_file_may_be_named(repo: Path) -> None:
    found = run(repo, {"shared/helpers.py": func("h"), "app/domain.py": func("mine")}, {"reference_paths": ["shared/helpers.py"]})
    assert [v.subject for v in found] == ["app.domain"]


def test_scanned_code_below_a_reference_path_or_vendor_folder_is_only_a_target(repo: Path) -> None:
    files = {"app/shared/lib.py": func("a") + "\n\n" + func("b"), "app/vendor/kit.py": func("c") + "\n\n" + func("d")}
    write_files(repo, files)
    policy = mini_policy(
        classify={"app": "domain", "app.shared": "domain", "app.vendor": "domain"},
        scan_vendors=True, duplicate_logic={"reference_paths": ["app/shared"]},
    )
    found, _ = run_own(policy, repo, services(), practices=False)
    assert not [v for v in found if v.rule == Rule.DUPLICATE_LOGIC]


def test_a_reference_path_inside_an_excluded_folder_is_still_read(repo: Path) -> None:
    write_files(repo, {"third/lib.py": func("h"), "app/domain.py": func("mine")})
    policy = mini_policy(exclude=["third"], duplicate_logic={"reference_paths": ["third"]})
    found, _ = run_own(policy, repo, services(), practices=False)
    assert [v.subject for v in found if v.rule == Rule.DUPLICATE_LOGIC] == ["app.domain"]


@pytest.mark.parametrize(
    "files",
    [{}, {"vendor/readme.txt": "x"}, {"vendor/broken.py": "def (:\n"}, {"vendor/.venv/lib.py": "x = 1\n"}, {"vendor/nul.py": "x = '\x00'\n\x00"}],
)
def test_a_reference_path_with_nothing_readable_is_a_finding_not_a_silent_pass(repo: Path, files: dict[str, str]) -> None:
    found = run(repo, files, REFERENCE)
    assert [(v.subject, v.detail) for v in found] == [("vendor", "`duplicate_logic.reference_paths` entry has no readable Python file")]


def test_an_unreadable_reference_file_beside_a_good_one_is_reported_not_skipped_silently(repo: Path) -> None:
    write_files(repo, {"vendor/ok.py": func("h"), "vendor/broken.py": "def (:\n"})
    (repo / "vendor" / "latin.py").write_bytes(b"x = '\xff'\n")
    detail = "reference file cannot be parsed, so copies of it are not seen"
    assert [(v.subject, v.detail) for v in run(repo, {}, REFERENCE)] == [("vendor/broken.py", detail), ("vendor/latin.py", detail)]
    with_copy = run(repo, {"app/domain.py": func("mine")}, REFERENCE)
    assert [v.subject for v in with_copy] == ["app.domain", "vendor/broken.py", "vendor/latin.py"]


@pytest.mark.parametrize("entry", ["../x", "/abs", ".", "", "C:/x"])
def test_reference_paths_outside_the_repository_are_refused(entry: str) -> None:
    with pytest.raises(PolicyError):
        mini_policy(duplicate_logic={"reference_paths": [entry]})


@pytest.mark.parametrize(
    "table",
    [{"min_statements": 0}, {"min_statements": True}, {"min_branch_repeats": 1}, {"enabled": 1}, {"reference_paths": "vendor"}, {"nope": 1}],
)
def test_a_malformed_table_is_a_policy_error(table: dict[str, Any]) -> None:
    with pytest.raises(PolicyError, match=r"duplicate_logic|reference_paths|enabled"):
        mini_policy(duplicate_logic=table)


def test_dev_tools_paths_are_validated_as_before() -> None:
    assert mini_policy(dev_tools=["./tools/"]).dev_tools == ("tools",)
    with pytest.raises(PolicyError, match=r"`dev_tools` entry '\.\.' must be a folder or file below the repository root"):
        mini_policy(dev_tools=[".."])


# --- branch skeletons -------------------------------------------------------------------------------------------------


def finder(name: str, thing: str) -> str:
    return (
        f"def {name}(key):\n    log({thing!r})\n    if hosted_enabled():\n        return remote.find(key)\n"
        "    else:\n        return local.find(key)\n"
    )


def guard_finder(name: str, thing: str) -> str:
    return f"def {name}(key):\n    log({thing!r})\n    if hosted_enabled():\n        return remote.find(key)\n    return local.find(key)\n"


SKELETONS = {"branch_skeletons": True}


def test_a_repeated_branch_is_reported_per_function_only_when_asked(repo: Path) -> None:
    files = {"app/domain.py": finder("users", "u") + finder("jobs", "j"), "app/ports.py": guard_finder("files", "f")}
    assert run(repo, files) == []
    found = run(repo, files, SKELETONS)
    assert [(v.subject, v.detail.split("`")[1]) for v in found] == [("app.domain", "users"), ("app.domain", "jobs"), ("app.ports", "files")]
    assert "hosted_enabled" in found[0].detail and "remote.find" in found[0].detail and "local.find" in found[0].detail
    assert len({v.detail.split("branch", 1)[1] for v in found}) == 1


def test_two_repeats_are_below_the_default_and_the_threshold_is_configurable(repo: Path) -> None:
    files = {"app/domain.py": finder("users", "u") + finder("jobs", "j")}
    assert run(repo, files, SKELETONS) == []
    assert len(run(repo, files, {**SKELETONS, "min_branch_repeats": 2})) == 2


def test_conditional_expressions_count_and_different_called_names_do_not_match(repo: Path) -> None:
    ternary = "def {0}(k):\n    return remote.find(k) if hosted_enabled() else local.find(k)\n"
    other = "def {0}(k):\n    return remote.drop(k) if hosted_enabled() else local.drop(k)\n"
    two = {"min_branch_repeats": 2, **SKELETONS}
    assert len(run(repo, {"app/domain.py": ternary.format("a") + ternary.format("b")}, two)) == 2
    assert run(repo, {"app/domain.py": ternary.format("a") + other.format("b")}, two) == []


def test_conditions_with_different_literals_and_branches_without_calls_do_not_match(repo: Path) -> None:
    branch = "def {0}(mode):\n    if mode == {1!r}:\n        one()\n    else:\n        two()\n"
    plain = "def {0}(flag):\n    if flag:\n        x = 1\n    else:\n        x = 2\n    return x\n"
    two = {"min_branch_repeats": 2, **SKELETONS}
    assert run(repo, {"app/domain.py": branch.format("a", "x") + branch.format("b", "y")}, two) == []
    assert run(repo, {"app/domain.py": plain.format("a") + plain.format("b")}, two) == []


def test_whole_copies_already_reported_count_once_for_the_skeleton(repo: Path) -> None:
    body = "def {0}(k):\n    a = k\n    b = a\n    if hosted_enabled():\n        remote.find(b)\n    else:\n        local.find(b)\n    return b\n"
    found = run(repo, {"app/domain.py": body.format("a") + body.format("b") + body.format("c")}, SKELETONS)
    assert [v.detail for v in found] == [f"function `{n}` duplicates logic defined elsewhere" for n in "abc"]


def test_the_same_branch_twice_in_one_function_is_one_function(repo: Path) -> None:
    twice = "def a(k):\n    x = f(k) if on() else g(k)\n    y = f(k) if on() else g(k)\n    return x, y\n"
    assert run(repo, {"app/domain.py": twice}, {"min_branch_repeats": 2, **SKELETONS}) == []


# --- baseline, ratchet, cache, callers --------------------------------------------------------------------------------


def test_the_baseline_ratchet_only_shrinks(repo: Path) -> None:
    policy = mini_policy(duplicate_logic={})
    svc = services()
    path = repo / "baseline.json"
    write_files(repo, {"app/domain.py": func("a") + "\n\n" + func("b") + "\n\n" + func("c")})
    old = blocking(run_own(policy, repo, svc, practices=False)[0])
    assert ratchet.record(svc.fs, path, old, ratchet.RecordOptions()) == 3
    keys = ratchet.load(svc.fs, path)

    write_files(repo, {"app/domain.py": func("a") + "\n\n" + func("b")})  # one copy removed: only its entry goes stale
    result = ratchet.apply(blocking(run_own(policy, repo, svc, practices=False)[0]), keys)
    assert result.new == [] and result.stale == ["duplicate-logic|app.domain|function `c` duplicates logic defined elsewhere"]

    write_files(repo, {"app/domain.py": func("a") + "\n\n" + func("b") + "\n\n" + func("c"), "app/ports.py": func("d")})
    result = ratchet.apply(blocking(run_own(policy, repo, svc, practices=False)[0]), keys)
    assert [v.subject for v in result.new] == ["app.ports"] and result.stale == []


def test_check_fails_on_a_new_duplicate_and_a_changed_only_run_does_not_judge_it(repo: Path) -> None:
    write_files(repo, {"app/domain.py": func("a") + "\n\n" + func("b")})
    policy = mini_policy(duplicate_logic={})
    report = run_check(policy, repo, services(), Options(tools=False))
    assert report.ratchet is not None and {v.rule for v in report.ratchet.new} >= {Rule.DUPLICATE_LOGIC}
    only, _ = run_own(policy, repo, services(), only=frozenset({"app/domain.py"}), practices=False)
    assert not [v for v in only if v.rule == Rule.DUPLICATE_LOGIC]  # a whole-repository fact


def test_a_named_ratchet_can_count_the_rule_per_module(repo: Path) -> None:
    write_files(repo, {"app/domain.py": func("a") + "\n\n" + func("b")})
    write_files(repo, {"arborescent-ratchets.json": '{"version": 1, "ratchets": {"dups": {}}, "growth": []}'})
    policy = mini_policy(duplicate_logic={}, ratchet={"dups": {"rule": "duplicate-logic"}})
    report = run_check(policy, repo, services(), Options(tools=False))
    (counted,) = [run for run in report.engines if run.name == "ratchet dups"]
    assert counted.status == "fail" and any("app.domain" in line for line in counted.lines)


def test_the_cli_records_and_then_accepts_the_findings(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {
        "app/domain.py": func("a") + "\n\n" + func("b"),
        "arborescent.toml": (
            'sources = ["app"]\nrings = ["domain"]\n[classify]\n"app" = "domain"\n[ring.domain]\nexternal = ["*"]\n'
            'may_import = ["*"]\n[duplicate_logic]\nmin_statements = 4\n'
        ),
    })
    config = str(repo / "arborescent.toml")
    assert main(["--config", config, "check", "--no-tools"]) == 1
    assert "[duplicate-logic]" in capsys.readouterr().out
    assert main(["--config", config, "baseline"]) == 0
    recorded = json.loads((repo / "arborescent-baseline.json").read_text(encoding="utf-8"))
    assert sum(key.startswith("duplicate-logic|") for key in recorded["violations"]) == 2
    assert main(["--config", config, "check", "--no-tools"]) == 0


def test_the_cache_is_used_and_survives_corruption_and_edits(repo: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    files = {"app/domain.py": func("a"), "app/ports.py": func("b")}
    first = run(repo, files, cache=cache)
    store = cache / "duplicates.json"
    assert store.is_file() and len(first) == 2
    stamp = store.read_text(encoding="utf-8")
    assert run(repo, {}, cache=cache) == first and store.read_text(encoding="utf-8") == stamp
    for junk in ("", "{", "[]", '{"v": 1}', json.dumps({**json.loads(stamp), "files": {"m:app.domain": [1]}})):
        store.write_text(junk, encoding="utf-8")
        assert run(repo, {}, cache=cache) == first
    assert run(repo, {"app/ports.py": "X = 1\n"}, cache=cache) == []


def test_selftest_kills_the_probes_and_a_disabled_rule_is_reported(repo: Path) -> None:
    write_files(repo, {"vendor/lib.py": "X = 1\n"})
    policy = mini_policy(duplicate_logic={"branch_skeletons": True, "reference_paths": ["vendor"]})
    report = selftest(policy, repo, services())
    assert len([k for k in report.killed if "[duplicate-logic]" in k]) == 2
    assert not [s for s in report.survived if "[duplicate-logic]" in s]
    mutants, skipped = plan(mini_policy(), services().scanner.scan(mini_policy(), repo))
    assert not [m for m in mutants if m.rule == Rule.DUPLICATE_LOGIC]
    assert "duplicate-logic: disabled by policy" in skipped


def test_a_reference_path_that_itself_lies_below_a_noise_directory_is_read(repo: Path) -> None:
    write_files(repo, {".venv/lib/evalkit/text.py": func("normalise"), "app/domain.py": func("mine")})
    found = run(repo, {}, {"reference_paths": [".venv/lib/evalkit"]})
    assert [v.subject for v in found] == ["app.domain"]


def test_the_selftest_probe_goes_into_a_module_the_scan_checks(repo: Path) -> None:
    write_files(repo, {"aaa/outside.py": "X = 1\n"})  # discovered and sorted first, but neither classified nor a source
    policy = mini_policy(duplicate_logic={})
    scan = services().scanner.scan(policy, repo)
    (probe,) = [m for m in plan(policy, scan)[0] if m.rule == Rule.DUPLICATE_LOGIC]
    assert probe.file in {scan.paths[name] for name in scan.modules}
