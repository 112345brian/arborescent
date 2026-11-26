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


# --- near misses ------------------------------------------------------------------------------------------------------

NEAR = {"near_misses": True, "min_near_statements": 5}


def near_body(name: str, **change: str) -> str:
    """Seven statements; ``change`` replaces or inserts one part."""
    part = {"head": "0", "sep": '"-"', "extra": "", "guard": "", "inner": "", "result": "joined", **change}
    head, sep, extra, guard, inner, result = (part[key] for key in ("head", "sep", "extra", "guard", "inner", "result"))
    return (
        f"def {name}(raw):\n{guard}    cleaned = raw.strip().lower()\n{extra}    parts = cleaned.split({sep})\n"
        f"    for part in parts:\n        check(part)\n{inner}    first = parts[{head}]\n    joined = first.title()\n    return {result}\n"
    )


def pair(left: str, right: str) -> dict[str, str]:
    return {"app/domain.py": left, "app/ports.py": right}


def test_near_misses_are_off_by_default(repo: Path) -> None:
    assert run(repo, pair(near_body("a"), near_body("b", head="1"))) == []
    assert not mini_policy(duplicate_logic={}).duplicate_logic.near_misses


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"head": "1"}, "one literal differs"),
        ({"sep": '"_"'}, "one literal differs"),
        ({"sep": '"\\x00\\udcff"'}, "one literal differs"),
        ({"extra": "    log(cleaned)\n"}, "one statement"),
        ({"extra": "    size = len(cleaned)\n"}, "one statement"),  # a new local shifts every later number
        ({"inner": "        note(part)\n"}, "one statement"),  # inside the loop
        ({"guard": "    if not raw:\n        return None\n"}, "one statement"),  # a guard clause is one extra step
    ],
)
def test_one_changed_literal_or_one_extra_statement_does_not_hide_a_copy(repo: Path, change: dict[str, str], said: str) -> None:
    found = run(repo, pair(near_body("a"), near_body("b", **change)), NEAR)
    assert [(v.subject, v.detail) for v in found] == [
        ("app.domain", "function `a` duplicates logic defined elsewhere"),
        ("app.ports", "function `b` duplicates logic defined elsewhere"),
    ]
    assert all(said in v.hint for v in found) and "app.ports:b" in found[0].hint and "app.domain:a" in found[1].hint


@pytest.mark.parametrize(
    "other",
    [
        near_body("b", head="1", sep='"_"'),  # two literals
        near_body("b", extra="    log(cleaned)\n    log(raw)\n"),  # two statements
        near_body("b", head="1", extra="    log(cleaned)\n"),  # one of each
        near_body("b").replace("title", "upper"),  # a called name
        near_body("b", result="first"),  # same statements wired differently
        near_body("b", head="1", result="first"),
        near_body("b", extra="    log(cleaned)\n", result="first"),
        near_body("b", head="1").replace("def b", "async def b"),
        near_body("b", extra="    for x in raw:\n        log(x)\n"),  # a whole loop is not "one statement"
    ],
)
def test_bigger_or_differently_wired_changes_are_not_near_copies(repo: Path, other: str) -> None:
    assert run(repo, pair(near_body("a"), other), NEAR) == []


def test_near_misses_have_their_own_larger_minimum(repo: Path) -> None:
    files = pair(near_body("a"), near_body("b", head="1"))
    assert run(repo, files, {"near_misses": True, "min_near_statements": 7}) != []
    assert run(repo, files, {"near_misses": True, "min_near_statements": 8}) == []
    default = mini_policy(duplicate_logic={}).duplicate_logic
    assert default.min_near_statements > default.min_statements


def test_a_function_gets_one_finding_and_diverging_a_copy_keeps_its_baseline_key(repo: Path) -> None:
    exact = pair(near_body("a"), near_body("b"))
    before = run(repo, exact, NEAR)
    after = run(repo, pair(near_body("a"), near_body("b", head="1")), NEAR)
    assert [v.key for v in before] == [v.key for v in after]
    both = run(repo, {**exact, "app/util.py": near_body("c", head="1")}, NEAR)
    assert sorted(v.detail.split("`")[1] for v in both) == ["a", "b", "c"]
    assert "same body" in both[0].hint and "one literal differs" in next(v for v in both if v.subject == "app.util").hint


def test_a_near_copy_of_a_vendored_helper_names_it_and_vendor_code_is_never_flagged(repo: Path) -> None:
    for change in ({"head": "1"}, {"extra": "    log(cleaned)\n"}):
        files = {"vendor/kit.py": near_body("helper"), "app/domain.py": near_body("mine", **change)}
        found = run(repo, files, {**NEAR, **REFERENCE})
        assert [v.subject for v in found] == ["app.domain"] and "vendor/kit.py:1 helper" in found[0].hint
    files = {"vendor/kit.py": near_body("helper", extra="    log(cleaned)\n"), "app/domain.py": near_body("mine")}
    assert [v.subject for v in run(repo, files, {**NEAR, **REFERENCE})] == ["app.domain"]  # the shorter side is ours
    only_vendor = {"vendor/kit.py": near_body("one") + near_body("two", head="1"), "app/domain.py": ""}
    assert run(repo, only_vendor, {**NEAR, **REFERENCE}) == []


def test_near_matching_stays_linear_on_the_worst_shapes(repo: Path) -> None:
    repeated = "def {0}(x):\n" + "    x += 1\n" * 3000 + "    return x\n"
    family = "".join(near_body(f"f{i}", head=str(i)) for i in range(3000))
    chain = "def g(x):\n    if x:\n        a()\n" + "    elif x:\n        a()\n" * 300 + "    return x\n"
    files = {"app/domain.py": repeated.format("a") + repeated.replace("return x", "return x + 1").format("b") + chain, "app/ports.py": family}
    start = time.perf_counter()
    found = run(repo, files, NEAR)
    assert sum(v.subject == "app.ports" for v in found) == 3000
    assert time.perf_counter() - start < 60


@pytest.mark.parametrize("source", ["", "def f():\n    ...\n", "x = 1\n"])
def test_near_matching_accepts_empty_and_stub_only_modules(repo: Path, source: str) -> None:
    assert run(repo, {"app/domain.py": source, "app/ports.py": near_body("a")}, NEAR) == []


def test_near_findings_survive_the_cache_and_a_record_of_the_old_form_is_a_miss(repo: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    files = pair(near_body("a"), near_body("b", extra="    log(cleaned)\n"))
    first = run(repo, files, NEAR, cache=cache)
    assert len(first) == 2 and run(repo, {}, NEAR, cache=cache) == first
    store = cache / "duplicates.json"
    document = json.loads(store.read_text(encoding="utf-8"))
    for record in document["files"].values():
        record[1] = [item[:5] for item in record[1]]
    store.write_text(json.dumps(document), encoding="utf-8")
    assert run(repo, {}, NEAR, cache=cache) == first


@pytest.mark.parametrize(
    "table", [{"near_misses": 1}, {"min_near_statements": 1}, {"min_near_statements": True}, {"branch_conditions": "yes"}],
)
def test_malformed_near_and_condition_keys_are_policy_errors(table: dict[str, Any]) -> None:
    with pytest.raises(PolicyError, match=r"duplicate_logic|near_misses|branch_conditions"):
        mini_policy(duplicate_logic=table)


# --- one decision repeated with different callees (#1622) -------------------------------------------------------------

CONDITIONS = {"branch_conditions": True}


def dispatch(name: str, *, test: str = "hosted_enabled()", params: str = "key") -> str:
    return f"def {name}({params}):\n    if {test}:\n        return remote.find_{name}(key)\n    return local.find_{name}(key)\n"


def test_finders_calling_differently_named_functions_on_one_decision_are_reported(repo: Path) -> None:
    files = {"app/domain.py": dispatch("users") + dispatch("jobs"), "app/ports.py": dispatch("files")}
    assert run(repo, files, SKELETONS) == []
    found = run(repo, files, CONDITIONS)
    assert [(v.subject, v.detail.split("`")[1]) for v in found] == [("app.domain", "users"), ("app.domain", "jobs"), ("app.ports", "files")]
    assert "branches on `hosted_enabled()`" in found[0].detail and "3 functions" in found[0].hint
    more = run(repo, {**files, "app/util.py": dispatch("extra")}, CONDITIONS)
    assert {v.key for v in found} < {v.key for v in more}  # a fourth copy adds one key and changes none


def test_finders_with_a_hosted_guard_and_their_own_local_filter_are_reported(repo: Path) -> None:
    source = "".join(
        f"def load_{name}(since):\n    if backend.hosted_enabled():\n        return backend.client().get_{name}(since)\n{rest}"
        for name, rest in (
            ("one", "    return pick_one(load_local())\n"),
            ("two", "    return [row for row in load_local() if wanted(row, since)]\n"),
            ("three", "    rows = load_local()\n    rows.sort()\n    return rows\n"),
        )
    )
    found = run(repo, {"app/domain.py": source}, CONDITIONS)
    assert [v.detail.split("`")[1] for v in found] == ["load_one", "load_two", "load_three"]


def test_the_other_forms_of_the_same_decision_count_together(repo: Path) -> None:
    forms = (
        "def a(k):\n    return remote.a(k) if hosted_enabled() else local.a(k)\n"
        "def b(k):\n    if not hosted_enabled():\n        return local.b(k)\n    else:\n        return remote.b(k)\n"
        "def c(k):\n    if hosted_enabled():\n        return remote.c(k)\n    row = db.get(k)\n    check(row)\n    return row\n"
    )
    assert [v.detail.split("`")[1] for v in run(repo, {"app/domain.py": forms}, CONDITIONS)] == ["a", "b", "c"]


@pytest.mark.parametrize("test", ['os.environ.get("HOSTED") == "1"', "settings.current().hosted", "config.hosted_enabled()"])
def test_conditions_that_depend_on_nothing_local_count(repo: Path, test: str) -> None:
    source = "".join(dispatch(name, test=test) for name in "abc")
    assert len(run(repo, {"app/domain.py": source}, CONDITIONS)) == 3


@pytest.mark.parametrize(
    "test",
    ["isinstance(key, str)", "key", "flag", "settings.hosted", 'sys.platform == "win32"', "key.hosted()", "check(key)", "(x := probe())"],
)
def test_conditions_on_the_arguments_or_without_a_call_do_not_count(repo: Path, test: str) -> None:
    source = "".join(dispatch(name, test=test) for name in "abc")
    assert run(repo, {"app/domain.py": source}, CONDITIONS) == []


def test_both_sides_must_call_different_things(repo: Path) -> None:
    same = "def {0}(k):\n    if hosted_enabled():\n        log(k, {0!r})\n    else:\n        log({0!r}, k)\n"
    one_sided = "def {0}(k):\n    if hosted_enabled():\n        log(k)\n    return {0!r}\n"
    for template in (same, one_sided):
        assert run(repo, {"app/domain.py": "".join(template.format(n) for n in "abc")}, CONDITIONS) == []


def test_a_decision_on_self_is_grouped_within_its_class_only(repo: Path) -> None:
    def cls(name: str, methods: str) -> str:
        return f"class {name}:\n" + "".join(
            f"    def {m}(self, k):\n        if self.hosted():\n            return self.remote_{m}(k)\n        return self.local_{m}(k)\n"
            for m in methods
        )

    assert run(repo, {"app/domain.py": cls("A", "ab") + cls("B", "c"), "app/ports.py": cls("A", "d")}, CONDITIONS) == []
    assert [v.detail.split("`")[1] for v in run(repo, {"app/domain.py": cls("A", "abc")}, CONDITIONS)] == ["A.a", "A.b", "A.c"]


def test_with_both_branch_options_a_function_is_reported_once_per_decision(repo: Path) -> None:
    source = "".join(finder(name, name) for name in ("a", "b", "c"))
    found = run(repo, {"app/domain.py": source}, {**SKELETONS, **CONDITIONS})
    assert len(found) == 3 and all("branches on" in v.detail for v in found)
    assert run(repo, {"app/domain.py": dispatch("a") + dispatch("b")}, CONDITIONS) == []
    assert len(run(repo, {"app/domain.py": dispatch("a") + dispatch("b")}, {**CONDITIONS, "min_branch_repeats": 2})) == 2


def test_long_guard_ladders_stay_linear_for_conditions(repo: Path) -> None:
    ladder = "def g(x):\n" + "    if probe():\n        return a()\n" * 4000 + "    return b()\n"
    start = time.perf_counter()
    assert run(repo, {"app/domain.py": ladder}, CONDITIONS) == []
    assert time.perf_counter() - start < 30


def test_selftest_probes_near_misses_and_branch_conditions(repo: Path) -> None:
    policy = mini_policy(duplicate_logic={"near_misses": True, "branch_conditions": True})
    report = selftest(policy, repo, services())
    assert len([k for k in report.killed if "[duplicate-logic]" in k]) == 3
    assert not [s for s in report.survived if "[duplicate-logic]" in s]
