"""``call_allowlist`` (#229): specific calls, and pairs of calls in one function, only the sanctioned modules may make."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, ring_with, services, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.facts import extract
from arborescent.rules import check_modules
from arborescent.scan_cache_codec import cached_facts, facts_to_json
from arborescent.selftest import plan, selftest

REPLACE = {"calls": ["os.replace"], "modules": ["app.adapters"], "reason": "use save_text_atomic"}
ATOMIC = {"calls": ["tempfile.mkstemp", "tempfile.NamedTemporaryFile"], "and_calls": ["os.replace"], "modules": ["app.adapters"]}
HEAD = "from __future__ import annotations\n\n"
OPEN = ring_with(domain={"external": ["*"], "traits": ["permits-effects"]})


def run(root: Path, *entries: dict[str, Any]) -> list[Violation]:
    policy = mini_policy(call_allowlist=list(entries), ring=OPEN)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def outside(found: list[Violation]) -> list[tuple[str, int]]:
    return [(v.subject, v.line) for v in found if v.rule == Rule.CALL_OUTSIDE_ALLOWLIST and v.line]


def idioms(found: list[Violation]) -> list[tuple[str, int]]:
    return [(v.subject, v.line) for v in found if v.rule == Rule.HAND_ROLLED_IDIOM and v.line]


@pytest.mark.parametrize(
    "body",
    [
        "import os\n\nos.replace('a', 'b')\n",
        "import os as o\n\no.replace('a', 'b')\n",
        "import os.path\n\nos.replace('a', 'b')\n",
        "from os import replace\n\nreplace('a', 'b')\n",
        "from os import replace as swap\n\nswap('a', 'b')\n",
        "def f() -> None:\n    from os import replace\n    replace('a', 'b')\n",
        "def f() -> None:\n    import os\n    os.replace('a', 'b')\n",
        "class C:\n    def m(self) -> None:\n        import os\n        os.replace('a', 'b')\n",
        "import os\n\nF = lambda: os.replace('a', 'b')\n",
    ],
)
def test_every_spelling_of_a_listed_call_is_reported(repo: Path, body: str) -> None:
    write_files(repo, {"app/domain.py": HEAD + body})
    found = run(repo, REPLACE)
    assert [subject for subject, _ in outside(found)] == ["app.domain"]
    detail = next(v.detail for v in found if v.rule == Rule.CALL_OUTSIDE_ALLOWLIST)
    assert "os.replace" in detail and "app.adapters" in detail and "use save_text_atomic" in detail and "ring domain" in detail


@pytest.mark.parametrize(
    "body",
    [
        "import os\n\nos.rename('a', 'b')\n",
        "def f(s: str) -> str:\n    return s.replace('a', 'b')\n",
        "from dataclasses import replace\n\nreplace(object())\n",
        "def replace() -> None:\n    pass\n\n\nreplace()\n",
        "import os\n\nswap = os.replace\n",
        # documented blind spots: a re-bound name, getattr, passing the function on, a star import, a method on a built value
        "import os\n\nswap = os.replace\nswap('a', 'b')\n",
        "import os\n\ngetattr(os, 'replace')('a', 'b')\n",
        "import functools\nimport os\n\nfunctools.partial(os.replace, 'a')\n",
        "from os import *\n\nreplace('a', 'b')\n",
    ],
)
def test_what_is_not_a_listed_call_is_left_alone(repo: Path, body: str) -> None:
    write_files(repo, {"app/domain.py": HEAD + body})
    assert outside(run(repo, REPLACE)) == []


def test_the_sanctioned_module_and_its_submodules_may_call_it(repo: Path) -> None:
    body = "import os\n\nos.replace('a', 'b')\n"
    write_files(repo, {"app/adapters.py": HEAD + body})
    assert outside(run(repo, REPLACE)) == []


def test_rings_restrict_too(repo: Path) -> None:
    body = HEAD + "import os\n\nos.replace('a', 'b')\n"
    write_files(repo, {"app/adapters.py": body, "app/domain.py": body})
    assert outside(run(repo, {"calls": ["os.replace"], "rings": ["adapters"]})) == [("app.domain", 5)]


def test_a_glob_covers_a_whole_module(repo: Path) -> None:
    body = HEAD + "import subprocess\nfrom subprocess import Popen\n\nsubprocess.run([])\nPopen([]).wait()\n"
    write_files(repo, {"app/domain.py": body})
    found = run(repo, {"calls": ["subprocess.*"], "modules": ["app.adapters"]})
    assert outside(found) == [("app.domain", 6), ("app.domain", 7)]


def test_a_first_party_target_is_found_through_relative_imports_and_at_home(repo: Path) -> None:
    write_files(repo, {
        "app/ports.py": "def build() -> None:\n    pass\n\n\nbuild()\n",
        "app/domain.py": "from .ports import build\n\nbuild()\n",
    })
    found = run(repo, {"calls": ["app.ports.build"], "modules": ["app.main"]})
    assert outside(found) == [("app.domain", 3), ("app.ports", 5)]


def test_each_further_call_in_a_module_is_a_new_baseline_entry(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os\n\nos.replace('a', 'b')\n"})
    first = [v for v in run(repo, REPLACE) if v.rule == Rule.CALL_OUTSIDE_ALLOWLIST]
    recorded = {v.key for v in first}
    write_files(repo, {"app/domain.py": "import os\n\n\nos.replace('a', 'b')\n"})  # moved: same key
    assert baseline.apply([v for v in run(repo, REPLACE) if v.rule == Rule.CALL_OUTSIDE_ALLOWLIST], recorded).ok
    write_files(repo, {"app/domain.py": "import os\n\nos.replace('a', 'b')\nos.replace('b', 'c')\n"})
    ratchet = baseline.apply([v for v in run(repo, REPLACE) if v.rule == Rule.CALL_OUTSIDE_ALLOWLIST], recorded)
    assert [v.line for v in ratchet.new] == [4] and not ratchet.stale


@pytest.mark.parametrize(
    "body",
    [
        "import os\nimport tempfile\n\n\ndef save() -> None:\n    fd, tmp = tempfile.mkstemp()\n    os.replace(tmp, 'x')\n",
        "from os import replace\nfrom tempfile import NamedTemporaryFile as NTF\n\n\ndef save() -> None:\n    with NTF() as f:\n        pass\n    replace(f.name, 'x')\n",
        "import os\nimport tempfile\n\n\nclass S:\n    async def save(self) -> None:\n        fd, tmp = tempfile.mkstemp()\n\n        def done() -> None:\n            os.replace(tmp, 'x')\n",
        "import os\nimport tempfile\n\nif True:\n    def save() -> None:\n        fd, tmp = tempfile.mkstemp()\n        os.replace(tmp, 'x')\n",
        "import os\nimport tempfile\n\n\nclass S:\n    class T:\n        def save(self) -> None:\n            fd, tmp = tempfile.mkstemp()\n            os.replace(tmp, 'x')\n",
    ],
)
def test_a_function_with_both_halves_of_an_idiom_is_reported(repo: Path, body: str) -> None:
    write_files(repo, {"app/domain.py": body})
    found = run(repo, ATOMIC)
    assert [subject for subject, _ in idioms(found)] == ["app.domain"]
    assert outside(found) == []  # either call alone is legitimate
    violation = next(v for v in found if v.rule == Rule.HAND_ROLLED_IDIOM)
    assert "save()" in violation.detail and "os.replace" in violation.detail and "app.adapters" in violation.detail


@pytest.mark.parametrize(
    "body",
    [
        "import os\nimport tempfile\n\n\ndef a() -> None:\n    tempfile.mkstemp()\n\n\ndef b() -> None:\n    os.replace('a', 'b')\n",
        "import os\nimport tempfile\n\ntempfile.mkstemp()\nos.replace('a', 'b')\n",  # module level: documented blind spot
        "import os\nimport tempfile\n\n\n@os.replace\ndef a() -> None:\n    tempfile.mkstemp()\n",  # a decorator runs outside
        "import tempfile\nfrom pathlib import Path\n\n\ndef a() -> None:\n    _, tmp = tempfile.mkstemp()\n    Path(tmp).replace('x')\n",
    ],
)
def test_an_idiom_needs_both_calls_in_one_function(repo: Path, body: str) -> None:
    write_files(repo, {"app/domain.py": body})
    assert idioms(run(repo, ATOMIC)) == []


def test_one_call_cannot_be_both_halves(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os\n\n\ndef a() -> None:\n    os.replace('a', 'b')\n"})
    entry = {"calls": ["os.*"], "and_calls": ["os.replace"], "modules": ["app.adapters"]}
    assert idioms(run(repo, entry)) == []


def test_the_sanctioned_module_may_hand_roll_the_idiom_and_same_named_functions_have_separate_keys(repo: Path) -> None:
    body = "import os\nimport tempfile\n\n\ndef save() -> None:\n    tempfile.mkstemp()\n    os.replace('a', 'b')\n"
    twice = body + "\n\ndef save() -> None:  # redefined\n    tempfile.mkstemp()\n    os.replace('a', 'b')\n"
    write_files(repo, {"app/adapters.py": body, "app/domain.py": twice})
    found = [v for v in run(repo, ATOMIC) if v.rule == Rule.HAND_ROLLED_IDIOM]
    assert [v.subject for v in found] == ["app.domain", "app.domain"] and len({v.key for v in found}) == 2


def test_an_owner_glob_that_matches_nothing_is_reported_under_the_rule_of_its_entry(repo: Path) -> None:
    write_files(repo, {"app/domain.py": ""})
    found = run(repo, {**REPLACE, "modules": ["app.gone*"]}, {**ATOMIC, "modules": ["app.gone2"]})
    stale = [(v.rule, v.detail) for v in found if v.rule in (Rule.CALL_OUTSIDE_ALLOWLIST, Rule.HAND_ROLLED_IDIOM)]
    assert [rule for rule, _ in stale] == [Rule.CALL_OUTSIDE_ALLOWLIST, Rule.HAND_ROLLED_IDIOM]
    assert "app.gone*" in stale[0][1] and "app.gone2" in stale[1][1]


@pytest.mark.parametrize(
    "value",
    [
        "x", [1], [{}], [{"modules": ["app"]}], [{"calls": [], "modules": ["app"]}], [{"calls": "os.replace", "modules": ["app"]}],
        [{"calls": [""], "modules": ["app"]}], [{"calls": ["open"], "modules": ["app"]}], [{"calls": ["os."], "modules": ["app"]}],
        [{"calls": ["os.re\x00place"], "modules": ["app"]}], [{"calls": ["*.replace"], "modules": ["app"]}], [{"calls": [1], "modules": ["app"]}],
        [{"calls": ["os.replace"]}], [{"calls": ["os.replace"], "modules": "app"}], [{"calls": ["os.replace"], "modules": [""]}],
        [{"calls": ["os.replace"], "rings": [1]}], [{"calls": ["os.replace"], "modules": ["a"], "reason": 1}],
        [{"calls": ["os.replace"], "modules": ["a"], "and_calls": []}], [{"calls": ["os.replace"], "modules": ["a"], "and_calls": "os.x"}],
        [{"calls": ["os.replace"], "modules": ["a"], "and_call": ["os.rename"]}],
    ],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="call_allowlist"):
        policy_from_mapping({**MINI, "call_allowlist": value})


def test_it_is_off_by_default_and_parsed_into_entries(repo: Path) -> None:
    assert mini_policy().call_allowlist == ()
    first, second = mini_policy(call_allowlist=[REPLACE, ATOMIC]).call_allowlist
    assert (first.calls, first.and_calls, first.modules, first.reason) == (("os.replace",), (), ("app.adapters",), "use save_text_atomic")
    assert second.and_calls == ("os.replace",)
    write_files(repo, {"app/domain.py": "import os\n\nos.replace('a', 'b')\n"})
    assert outside(run(repo)) == []


def test_it_works_per_scope() -> None:
    scope = {"sources": ["app"], "source_roots": ["."], "rings": ["domain"], "classify": {"app": "domain"}, "call_allowlist": [REPLACE]}
    policy = policy_from_mapping({"scopes": {"one": scope}})
    assert len(policy.scopes[0].policy.call_allowlist) == 1
    with pytest.raises(PolicyError, match="call_allowlist"):
        policy_from_mapping({"scopes": {"one": scope}, "call_allowlist": [REPLACE]})


def test_function_spans_survive_the_scan_cache() -> None:
    facts = extract("import os\n\n\ndef f() -> None:\n    os.replace('a', 'b')\n\n\nclass C:\n    def m(self) -> None:\n        pass\n")
    assert [(s.name, s.line, s.end_line) for s in facts.function_spans] == [("f", 4, 5), ("C.m", 9, 10)]
    again = cached_facts(["digest", facts_to_json(facts)])
    assert again is not None and again.function_spans == facts.function_spans and again == facts


def test_empty_source_has_no_function_spans() -> None:
    assert extract("").function_spans == ()


def test_selftest_probes_each_entry_even_where_the_host_already_violates_it(repo: Path) -> None:
    body = HEAD + "import os\nimport tempfile\n\n\ndef save() -> None:\n    tempfile.mkstemp()\n    os.replace('a', 'b')\n"
    write_files(repo, {"app/domain.py": body})
    entries = [REPLACE, ATOMIC, {"calls": ["subprocess.*"], "modules": ["app.adapters"]}, {"calls": ["os.exec*"], "rings": ["adapters"]}]
    policy = mini_policy(call_allowlist=entries, ring=OPEN)
    mutations = [m for m in plan(policy, FsScanner().scan(policy, repo))[0] if m.rule in (Rule.CALL_OUTSIDE_ALLOWLIST, Rule.HAND_ROLLED_IDIOM)]
    assert [m.rule for m in mutations] == [Rule.CALL_OUTSIDE_ALLOWLIST, Rule.HAND_ROLLED_IDIOM, Rule.CALL_OUTSIDE_ALLOWLIST, Rule.CALL_OUTSIDE_ALLOWLIST]
    assert all(m.file != "app/adapters.py" for m in mutations)
    report = selftest(replace(policy, type_checkers=()), repo, services())
    for rule in ("call-outside-allowlist", "hand-rolled-idiom"):
        assert any(entry.startswith(f"[{rule}]") for entry in report.killed), (rule, report.survived)
        assert not any(entry.startswith(f"[{rule}]") for entry in report.survived)


def test_selftest_says_why_an_entry_every_module_may_call_has_no_probe(repo: Path) -> None:
    policy = mini_policy(call_allowlist=[{"calls": ["os.replace"], "modules": ["app*"]}])
    mutations, skipped = plan(policy, FsScanner().scan(policy, repo))
    assert not [m for m in mutations if m.rule == Rule.CALL_OUTSIDE_ALLOWLIST]
    assert any(text.startswith("call_allowlist os.replace:") for text in skipped)


PATHLIB = {"calls": ["pathlib.Path.replace", "pathlib.Path.rename"], "modules": ["app.adapters"]}


@pytest.mark.parametrize(
    "body",
    [
        "from pathlib import Path\n\nPath('a').replace('b')\n",
        "import pathlib\n\npathlib.Path('a').rename('b')\n",
        "from pathlib import Path as P\n\nP('a').replace(target='b')\n",
        "from pathlib import Path\n\n\ndef f(tmp_path: Path) -> None:\n    tmp_path.rename('b')\n",
        "from pathlib import Path\n\n\ndef f(p: 'Path | None') -> None:\n    p.replace('b')\n",
        "from typing import Optional\nfrom pathlib import Path\n\n\ndef f(p: Optional[Path]) -> None:\n    p.replace('b')\n",
        "from pathlib import Path\n\n\ndef f(name: str) -> None:\n    tmp = Path(name)\n    tmp.replace('b')\n",
        "from pathlib import Path\n\n\ndef f(dest: Path) -> None:\n    tmp = dest.with_suffix('.tmp')\n    tmp.replace(dest)\n",
        "from pathlib import Path\n\n\ndef f(root: Path) -> None:\n    (root / 'a' / 'b').replace(root)\n",
        "from pathlib import Path\n\n\ndef f(root: Path) -> None:\n    root.parent.joinpath('a').resolve().rename(root)\n",
        "from pathlib import Path\n\nTMP = Path.cwd() / 'x'\n\n\ndef f() -> None:\n    TMP.replace('b')\n",
        "from dataclasses import dataclass\nfrom pathlib import Path\n\n\n@dataclass\nclass C:\n    tmp: Path\n\n    def f(self) -> None:\n        self.tmp.replace('b')\n",
        "from pathlib import Path\n\n\ndef f(p: Path) -> None:\n    p = p.with_name('c')\n    p.replace('b')\n",
        "from pathlib import Path\n\n\ndef f() -> None:\n    p: Path = make()\n    p.replace('b')\n",
        "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from pathlib import Path\n\n\ndef f(p: Path) -> None:\n    p.replace('b')\n",
    ],
)
def test_a_method_on_a_value_known_to_be_a_path_is_a_pathlib_call(repo: Path, body: str) -> None:
    write_files(repo, {"app/domain.py": body})
    assert [subject for subject, _ in outside(run(repo, PATHLIB))] == ["app.domain"]


@pytest.mark.parametrize(
    "body",
    [
        "from pathlib import Path\n\n\ndef f(s: str) -> str:\n    return s.replace('a', 'b')\n",
        "from pathlib import Path\n\n\ndef f(s) -> str:\n    return s.replace('a')\n",  # nothing says what `s` is
        "from pathlib import Path\n\n\ndef f(s: 'str | Path') -> None:\n    s.replace('a')\n",
        "from pathlib import Path\n\n\ndef f(p: Path) -> str:\n    return str(p).replace('a', 'b')\n",
        "from pathlib import Path\n\n\ndef f(p: Path) -> str:\n    return p.name.replace('a', 'b')\n",
        "from pathlib import Path\n\n\ndef f(p: Path) -> str:\n    return p.as_posix().replace('a', 'b')\n",
        "from pathlib import Path\n\n\ndef f(s: str) -> str:\n    s = s.replace('a', 'b')\n    return s\n",
        "from pathlib import Path\n\n\ndef f(items) -> None:\n    p = Path('a')\n    for p in items:\n        p.replace('b')\n",
        "from pathlib import Path\n\n\ndef f(s) -> None:\n    if s:\n        s = Path('a')\n    s.replace('b')\n",
        "from pathlib import Path\n\np = Path('a')\n\n\ndef f(p: str) -> None:\n    p.replace('b')\n",  # the parameter shadows it
        "from fastapi import Path\n\n\ndef f(p: Path) -> None:\n    p.replace('b')\n",
        "class Path:\n    pass\n\n\ndef f(p: Path) -> None:\n    p.replace('b')\n",
        "from datetime import datetime\nfrom pathlib import Path\n\n\ndef f(when: datetime) -> None:\n    when.replace(year=1)\n",
        "from pathlib import Path\n\n\ndef f(p: list[Path]) -> None:\n    p.rename('b')\n",
    ],
)
def test_a_replace_on_anything_else_is_left_alone(repo: Path, body: str) -> None:
    write_files(repo, {"app/domain.py": body})
    assert outside(run(repo, PATHLIB)) == []


def test_the_unbound_form_is_reported_once(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from pathlib import Path\n\n\ndef f(p: Path) -> None:\n    Path.replace(p, 'b')\n"})
    assert outside(run(repo, PATHLIB)) == [("app.domain", 5)]


def test_a_pathlib_atomic_write_is_a_hand_rolled_idiom(repo: Path) -> None:
    entry = {"calls": ["tempfile.mkstemp", "pathlib.Path.write_text"], "and_calls": ["pathlib.Path.replace"], "modules": ["app.adapters"]}
    body = (
        "import tempfile\nfrom pathlib import Path\n\n\ndef save() -> None:\n    fd, name = tempfile.mkstemp()\n    Path(name).replace('b')\n\n\n"
        "def other(dest: Path) -> None:\n    tmp = dest.with_suffix('.tmp')\n    tmp.write_text('x')\n    tmp.replace(dest)\n\n\n"
        "def fine(s: str, dest: Path) -> None:\n    dest.write_text(s.replace('a', 'b'))\n"
    )
    write_files(repo, {"app/domain.py": body})
    assert idioms(run(repo, entry)) == [("app.domain", 5), ("app.domain", 10)]


@pytest.mark.parametrize(
    "body",
    [
        "from pathlib import Path\n\np = Path('a')" + " / 'b'" * 500 + "\np.replace('c')\n",
        "from pathlib import Path\n\na = b\nb = a\na.replace('c')\n",
        "from pathlib import Path\n\n" + "".join(f"v{i} = v{i + 1}\n" for i in range(3000)) + "v0.replace('c')\n",
        "from pathlib import Path\n\nPath.nothing\n''.replace('a', 'b')\n",
        "import pathlib\n",
        "",
    ],
)
def test_odd_path_expressions_do_not_crash(body: str) -> None:
    assert all(name.startswith("pathlib.Path.") for _, name in extract(body).path_calls)


def test_the_path_calls_survive_the_scan_cache() -> None:
    facts = extract("from pathlib import Path\n\nPath('a').replace('b')\n")
    assert facts.path_calls == ((3, "pathlib.Path.replace"),)
    assert cached_facts(["digest", facts_to_json(facts)]) == facts


def test_selftest_probes_a_pathlib_entry_with_a_path_value(repo: Path) -> None:
    write_files(repo, {"app/domain.py": HEAD, "app/adapters.py": HEAD})
    policy = mini_policy(call_allowlist=[PATHLIB], ring=OPEN)
    [mutation] = [m for m in plan(policy, FsScanner().scan(policy, repo))[0] if m.rule == Rule.CALL_OUTSIDE_ALLOWLIST]
    assert '.Path("_arborescent_probe").replace(' in mutation.append
    report = selftest(replace(policy, type_checkers=()), repo, services())
    assert sum(entry.startswith("[call-outside-allowlist]") for entry in report.killed) == 1, report.survived
