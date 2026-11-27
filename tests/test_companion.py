"""``companion`` (#231): a function that does an action must also record it, itself or in a function it calls."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, ring_with, services, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.engine import run_own
from arborescent.facts import extract
from arborescent.rules import check_modules
from arborescent.scan_cache_codec import cached_facts, facts_to_json
from arborescent.selftest import plan, selftest

OPEN = ring_with(domain={"external": ["*"], "traits": ["permits-effects"]}, adapters={"may_import": ["domain", "ports"], "external": ["*"], "traits": ["permits-effects"]})
PAIR = {"when_calls": "app.ports.submit", "must_also_call": "app.ports.record", "reason": "every submission is recorded"}
PORTS = "def submit() -> None:\n    pass\n\n\ndef record() -> None:\n    pass\n"


def run(root: Path, *entries: dict[str, Any]) -> list[Violation]:
    policy = mini_policy(companion=list(entries) or [PAIR], ring=OPEN)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def missing(found: list[Violation]) -> list[tuple[str, int]]:
    return [(v.subject, v.line) for v in found if v.rule == Rule.MISSING_COMPANION_CALL and v.line]


def write(root: Path, body: str, **more: str) -> None:
    write_files(root, {"app/ports.py": PORTS, "app/domain.py": body, **{f"app/{name}.py": text for name, text in more.items()}})


@pytest.mark.parametrize(
    "body",
    [
        "from app.ports import submit\n\n\ndef go() -> None:\n    submit()\n",
        "from app.ports import submit as send\n\n\ndef go() -> None:\n    send()\n",
        "import app.ports\n\n\ndef go() -> None:\n    app.ports.submit()\n",
        "from . import ports\n\n\ndef go() -> None:\n    ports.submit()\n",
        "from .ports import submit\n\n\ndef go() -> None:\n    submit()\n",
        "def go() -> None:\n    from app.ports import submit\n    submit()\n",
        "from app.ports import submit, record\n\n\ndef go() -> None:\n    submit()\n\n\ndef other() -> None:\n    record()\n",
    ],
)
def test_the_action_without_the_record_is_reported(repo: Path, body: str) -> None:
    write(repo, body)
    found = run(repo)
    assert [subject for subject, _ in missing(found)] == ["app.domain"]
    violation = next(v for v in found if v.rule == Rule.MISSING_COMPANION_CALL)
    for text in ("go()", "app.ports.submit", "app.ports.record", "every submission is recorded", "ring domain"):
        assert text in violation.detail
    assert "companion-exempt" in violation.render()


@pytest.mark.parametrize(
    "body",
    [
        # the same function, directly, in either order
        "from app.ports import submit, record\n\n\ndef go() -> None:\n    submit()\n    record()\n",
        "from app.ports import submit, record\n\n\ndef go(ok: bool) -> None:\n    submit()\n    record()\n    if ok:\n        return\n",
        # one hop: a helper in this module, a method of this class, an imported helper, a static method of a class here
        "from app.ports import submit, record\n\n\ndef note() -> None:\n    record()\n\n\ndef go() -> None:\n    submit()\n    note()\n",
        "from app.ports import submit, record\n\n\nclass C:\n    def note(self) -> None:\n        record()\n\n    def go(self) -> None:\n        submit()\n        self.note()\n",
        "from app.ports import submit, record\n\n\nclass C:\n    @classmethod\n    def note(cls) -> None:\n        record()\n\n    @classmethod\n    def go(cls) -> None:\n        submit()\n        cls.note()\n",
        "from app.ports import submit, record\n\n\nclass C:\n    @staticmethod\n    def note() -> None:\n        record()\n\n\ndef go() -> None:\n    submit()\n    C.note()\n",
        "from app.ports import submit\nfrom app.adapters import note\n\n\ndef go() -> None:\n    submit()\n    note()\n",
        "from app.ports import submit\nfrom . import adapters\n\n\ndef go() -> None:\n    submit()\n    adapters.note()\n",
        "from app.ports import submit\nfrom app.adapters import Log\n\n\ndef go() -> None:\n    submit()\n    Log.note()\n",
    ],
)
def test_the_record_in_the_function_or_one_hop_away_is_enough(repo: Path, body: str) -> None:
    helpers = "from .ports import record\n\n\ndef note() -> None:\n    record()\n\n\nclass Log:\n    @staticmethod\n    def note() -> None:\n        record()\n"
    write(repo, body, adapters=helpers)
    assert missing(run(repo)) == []


def test_two_hops_is_too_far(repo: Path) -> None:
    body = (
        "from app.ports import submit, record\n\n\ndef inner() -> None:\n    record()\n\n\ndef outer() -> None:\n    inner()\n\n\n"
        "def go() -> None:\n    submit()\n    outer()\n"
    )
    write(repo, body)
    assert missing(run(repo)) == [("app.domain", 13)]


def test_a_method_form_matches_a_call_on_any_receiver(repo: Path) -> None:
    entry = {"when_calls": "*.submit", "must_also_call": "*.record_outcome"}
    body = (
        "class C:\n    def good(self, form: object) -> None:\n        form.submit()\n        self.recorder.record_outcome()\n\n"
        "    def built(self) -> None:\n        make().submit()\n        make().record_outcome()\n\n"
        "    def hop(self, form: object) -> None:\n        form.submit()\n        self.note()\n\n"
        "    def note(self) -> None:\n        self.recorder.record_outcome()\n\n"
        "    def bad(self, form: object) -> None:\n        form.submit()\n\n\ndef make() -> object:\n    return object()\n"
    )
    write(repo, body)
    assert missing(run(repo, entry)) == [("app.domain", 18)]


def test_a_dotted_target_does_not_match_a_method_on_a_local_value(repo: Path) -> None:
    write(repo, "def go(app: object) -> None:\n    app.ports.submit()\n")  # a parameter, not the module
    assert missing(run(repo)) == []


def test_any_of_several_recorders_will_do(repo: Path) -> None:
    entry = {"when_calls": ["app.ports.submit"], "must_also_call": ["app.ports.record_failure", "app.ports.record"]}
    write(repo, "from app.ports import submit, record\n\n\ndef go() -> None:\n    submit()\n    record()\n")
    assert missing(run(repo, entry)) == []


@pytest.mark.parametrize(
    "body",
    [
        "from app.ports import submit\n\nsubmit()\n",  # module level is not a function
        "from app.ports import submit\n\nsend = submit\n\n\ndef go() -> None:\n    send()\n",  # a re-bound name
        "from app.ports import submit\n\n\n@submit\ndef go() -> None:\n    pass\n",  # a decorator runs outside the function
    ],
)
def test_what_is_not_a_function_calling_the_action_is_left_alone(repo: Path, body: str) -> None:
    write(repo, body)
    assert missing(run(repo)) == []


@pytest.mark.parametrize(
    "body",
    [
        "from app.ports import submit\n\n\ndef go() -> None:  # companion-exempt: dry run\n    submit()\n",
        "from app.ports import submit\n\n\n# companion-exempt: dry run\ndef go() -> None:\n    submit()\n",
        "from app.ports import submit\n\n\n# companion-exempt: dry run\n@staticmethod\ndef go() -> None:\n    submit()\n",
        "from app.ports import submit\n\n\ndef go() -> None:\n    submit()  # companion-exempt: dry run\n",
        "from app.ports import submit\n\n\ndef go() -> None:\n    # companion-exempt: dry run, nothing leaves the machine\n    # (see the runbook)\n    submit()\n",
        "from app.ports import submit\n\n\ndef go(\n    flag: bool = False,\n) -> None:  # companion-exempt: dry run\n    submit()\n",
    ],
)
def test_an_exemption_comment_with_a_reason_exempts_the_function(repo: Path, body: str) -> None:
    write(repo, body)
    assert missing(run(repo)) == []


@pytest.mark.parametrize(
    "body",
    [
        "from app.ports import submit\n\n\ndef go() -> None:  # companion-exempt:\n    submit()\n",  # no reason
        "from app.ports import submit\n\n\ndef go() -> None:  # companion-exempt:   \n    submit()\n",
        "from app.ports import submit\n\n\ndef go() -> None:  # companion-exempt\n    submit()\n",
        "from app.ports import submit\n\n\n# companion-exempt: dry run\n\ndef go() -> None:\n    submit()\n",  # a blank line ends it
        "from app.ports import submit\n\nNOTE = '# companion-exempt: a string'\n\n\ndef go() -> None:\n    submit()\n",
        "from app.ports import submit\n\n\ndef other() -> None:  # companion-exempt: other one\n    pass\n\n\ndef go() -> None:\n    submit()\n",
    ],
)
def test_an_exemption_without_a_reason_or_elsewhere_does_not_exempt(repo: Path, body: str) -> None:
    write(repo, body)
    assert [subject for subject, _ in missing(run(repo))] == ["app.domain"]


def test_modules_and_rings_scope_the_rule(repo: Path) -> None:
    body = "from app.ports import submit\n\n\ndef go() -> None:\n    submit()\n"
    write(repo, body, adapters=body)
    assert missing(run(repo, {**PAIR, "rings": ["adapters"]})) == [("app.adapters", 5)]
    assert missing(run(repo, {**PAIR, "modules": ["app.dom*"]})) == [("app.domain", 5)]
    assert missing(run(repo, {**PAIR, "modules": ["app.domain"], "rings": ["adapters"]})) == []


def test_a_scope_glob_that_matches_nothing_is_reported(repo: Path) -> None:
    write(repo, "")
    found = run(repo, {**PAIR, "modules": ["app.gone*"]})
    [stale] = [v for v in found if v.rule == Rule.MISSING_COMPANION_CALL]
    assert stale.line == 0 and "app.gone*" in stale.detail


def test_each_function_has_its_own_baseline_entry_that_survives_a_move(repo: Path) -> None:
    head = "from app.ports import submit\n\n\n"
    write(repo, head + "class A:\n    def go(self) -> None:\n        submit()\n")
    recorded = {v.key for v in run(repo) if v.rule == Rule.MISSING_COMPANION_CALL}
    write(repo, head + "\n\nclass A:\n    def go(self) -> None:\n        submit()\n        submit()\n\n\ndef go() -> None:\n    submit()\n")
    ratchet = baseline.apply([v for v in run(repo) if v.rule == Rule.MISSING_COMPANION_CALL], recorded)
    assert [v.detail.split("] (ring domain) ")[1].split("()")[0] for v in ratchet.new] == ["go"] and not ratchet.stale


def test_two_entries_report_separately(repo: Path) -> None:
    write(repo, "from app.ports import submit\n\n\ndef go() -> None:\n    submit()\n")
    other = {"when_calls": "app.ports.submit", "must_also_call": "app.ports.audit"}
    assert len({v.key for v in run(repo, PAIR, other) if v.rule == Rule.MISSING_COMPANION_CALL}) == 2


def test_a_changed_only_run_reads_the_helper_it_did_not_select(repo: Path) -> None:
    """One hop looks into another module, so a partial run must not report a caller whose recording helper did not change."""
    write(repo, "from app.ports import submit\nfrom app.adapters import note\n\n\ndef go() -> None:\n    submit()\n    note()\n",
          adapters="from .ports import record\n\n\ndef note() -> None:\n    record()\n")
    policy = mini_policy(companion=[PAIR], ring=OPEN)
    found, _ = run_own(policy, repo, services(), only=frozenset({"app/domain.py"}), practices=False)
    assert missing(found) == []


@pytest.mark.parametrize(
    "value",
    [
        "x", [1], [{}], [{"when_calls": "a.b"}], [{"must_also_call": "a.b"}], [{**PAIR, "when_calls": ""}], [{**PAIR, "when_calls": []}],
        [{**PAIR, "when_calls": "submit"}], [{**PAIR, "when_calls": ["a.b", ""]}], [{**PAIR, "must_also_call": 1}],
        [{**PAIR, "must_also_call": "*."}], [{**PAIR, "must_also_call": "*.a.b"}], [{**PAIR, "must_also_call": "*.*"}],
        [{**PAIR, "must_also_call": "a.b\x00"}], [{**PAIR, "modules": "app"}], [{**PAIR, "modules": [""]}], [{**PAIR, "rings": [1]}],
        [{**PAIR, "reason": 1}], [{**PAIR, "hops": 2}],
    ],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="companion"):
        policy_from_mapping({**MINI, "companion": value})


def test_it_is_off_by_default_and_parsed_into_entries(repo: Path) -> None:
    assert mini_policy().companion == ()
    [entry] = mini_policy(companion=[PAIR]).companion
    assert (entry.when_calls, entry.must_also_call, entry.modules, entry.rings) == (("app.ports.submit",), ("app.ports.record",), (), ())
    write(repo, "from app.ports import submit\n\n\ndef go() -> None:\n    submit()\n")
    policy = mini_policy(ring=OPEN)
    scan = FsScanner().scan(policy, repo)
    assert missing(check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)) == []


def test_it_works_per_scope() -> None:
    scope = {"sources": ["app"], "source_roots": ["."], "rings": ["domain"], "classify": {"app": "domain"}, "companion": [PAIR]}
    assert len(policy_from_mapping({"scopes": {"one": scope}}).scopes[0].policy.companion) == 1
    with pytest.raises(PolicyError, match="companion"):
        policy_from_mapping({"scopes": {"one": scope}, "companion": [PAIR]})


def test_the_method_calls_and_exemptions_survive_the_scan_cache() -> None:
    source = (
        "import os\n\n\n@dec\ndef f(x) -> None:  # companion-exempt: why\n    x.go()\n    self.a.go()\n    make().go()\n    os.getpid()\n    x.go()\n"
    )
    facts = extract(source)
    [span] = facts.function_spans
    assert (span.start, span.line, span.end_line) == (4, 5, 10)
    assert span.method_calls == ((6, "x.go"), (7, "self.a.go"), (8, "?.go"))  # an imported receiver is in `calls`, not here
    assert facts.companion_exempt == ((5, "why"),)
    again = cached_facts(["digest", facts_to_json(facts)])
    assert again == facts


def test_a_file_that_does_not_tokenize_has_no_exemptions_and_does_not_crash() -> None:
    facts = extract("# companion-exempt: why\ndef f():\n    pass\nx = 1\r\r\n")
    assert facts.companion_exempt == (() if facts.untokenizable else ((2, "why"),))  # 3.11 tokenizes a doubled CR
    assert extract("").companion_exempt == ()


def test_selftest_does_the_action_without_the_record(repo: Path) -> None:
    write(repo, "from __future__ import annotations\n")
    entries = [PAIR, {"when_calls": "*.send", "must_also_call": "*.audit", "rings": ["adapters"]}]
    policy = mini_policy(companion=entries, ring=OPEN)
    mutations = [m for m in plan(policy, FsScanner().scan(policy, repo))[0] if m.rule == Rule.MISSING_COMPANION_CALL]
    assert len(mutations) == 2 and mutations[1].file == "app/adapters.py"
    report = selftest(replace(policy, type_checkers=()), repo, services())
    assert sum(entry.startswith("[missing-companion-call]") for entry in report.killed) == 2, report.survived
    assert not any(entry.startswith("[missing-companion-call]") for entry in report.survived)


def test_selftest_says_why_an_entry_with_no_module_in_scope_has_no_probe(repo: Path) -> None:
    policy = mini_policy(companion=[{**PAIR, "modules": ["nothing.here"]}])
    mutations, skipped = plan(policy, FsScanner().scan(policy, repo))
    assert not [m for m in mutations if m.rule == Rule.MISSING_COMPANION_CALL]
    assert any(text.startswith("companion app.ports.submit:") for text in skipped)


def test_a_changed_only_run_still_reports_a_caller_whose_only_unread_callee_is_the_action(repo: Path) -> None:
    write(repo, "from app.ports import submit\n\n\ndef go() -> None:\n    submit()\n")
    policy = mini_policy(companion=[PAIR], ring=OPEN)
    found, _ = run_own(policy, repo, services(), only=frozenset({"app/domain.py"}), practices=False)
    assert missing(found) == [("app.domain", 5)]


HEADER = "from app.ports import submit, record\n\n\n"


def function(body: str) -> str:
    return HEADER + "def go(x, y, items, rec=None) -> None:\n" + "".join(f"    {line}\n" for line in body.splitlines())


@pytest.mark.parametrize(
    "body",
    [
        # if / else and early return
        "submit()\nif x:\n    record()",
        "if x:\n    submit()\nelse:\n    record()",
        "if x:\n    record()\n    return\nsubmit()",
        "submit()\nif x:\n    return\nrecord()",
        "submit()\nif x:\n    record()\nelif y:\n    pass\nelse:\n    record()",
        "if submit():\n    record()",
        "if x:\n    submit()\nif y:\n    record()",
        "if x and y:\n    submit()\nif x or y:\n    record()",
        "if x:\n    submit()\nif not x:\n    record()",
        "if x:\n    submit()\nx = y\nif x:\n    record()",  # `x` changed, so the second test says nothing
        # try / except / else / finally
        "try:\n    submit()\nexcept OSError:\n    record()",
        "submit()\ntry:\n    x.check()\nexcept OSError:\n    return\nrecord()",
        "try:\n    submit()\n    if x:\n        raise ValueError\n    record()\nexcept ValueError:\n    pass",
        "try:\n    submit()\nexcept OSError:\n    return\nelse:\n    if x:\n        record()",
        "try:\n    submit()\n    if x:\n        return\n    record()\nfinally:\n    y.close()",
        # match
        "submit()\nmatch x:\n    case 'a':\n        record()",
        "submit()\nmatch x:\n    case 'a':\n        record()\n    case _:\n        pass",
        "submit()\nmatch x:\n    case 'a':\n        record()\n    case 'b':\n        record()",  # strings are not exhaustive
        # loops
        "for item in items:\n    submit()\n    if item:\n        continue\n    record()",
        "submit()\nfor item in items:\n    if item:\n        record()\n        break",
        "while x:\n    submit()\n    if y:\n        return\n    record()",
        "while True:\n    submit()\n    if y:\n        break\n    record()",
        # a guard that is not the recorder
        "submit()\nif x is not None:\n    record()",
    ],
)
def test_a_path_that_skips_the_record_is_reported(repo: Path, body: str) -> None:
    write(repo, function(body))
    found = run(repo)
    assert [subject for subject, _ in missing(found)] == ["app.domain"]
    violation = next(v for v in found if v.rule == Rule.MISSING_COMPANION_CALL)
    assert "on a path" in violation.detail


@pytest.mark.parametrize(
    "body",
    [
        "submit()\nif x:\n    record()\nelse:\n    record()",
        "if x:\n    submit()\n    record()",
        "if not x:\n    return\nsubmit()\nrecord()",
        "record()\nsubmit()\nif x:\n    return",
        "submit()\nif x:\n    raise ValueError\nrecord()",  # leaving by an exception is not a finished path
        "submit()\nif x:\n    sys.exit(1)\nrecord()",
        "submit()\nif x:\n    record()\nelse:\n    assert_never(x)",
        # the same condition twice
        "if x:\n    submit()\nif x:\n    record()",
        "if not x:\n    submit()\nif not x:\n    record()",
        "if x and not y:\n    submit()\nif x and not y:\n    record()",
        "if x.dry is None:\n    submit()\nif x.dry is not None:\n    return\nrecord()",
        # try
        "try:\n    submit()\n    record()\nexcept OSError:\n    pass",
        "try:\n    submit()\nexcept OSError:\n    return\nrecord()",
        "try:\n    submit()\nexcept OSError:\n    record()\n    raise\nelse:\n    record()",
        "submit()\ntry:\n    record()\nexcept Exception:\n    pass",  # best-effort recording
        "submit()\ntry:\n    x.prepare()\n    record()\nexcept Exception:\n    pass",
        "try:\n    submit()\n    if x:\n        return\nfinally:\n    record()",
        "try:\n    submit()\nexcept* OSError:\n    pass\nfinally:\n    record()",
        # match
        "submit()\nmatch x:\n    case 'a':\n        record()\n    case _:\n        record()",
        "submit()\nmatch x:\n    case S.A:\n        record()\n    case S.B | S.C:\n        record()",
        "submit()\nmatch x:\n    case Ok():\n        record()\n    case Err():\n        record()",
        "submit()\nmatch x:\n    case 'a':\n        record()\n    case other:\n        record()",
        # loops run at least once
        "submit()\nfor item in items:\n    record()",
        "for item in items:\n    submit()\nfor item in items:\n    record()",
        "for attempt in items:\n    try:\n        submit()\n        break\n    except OSError:\n        continue\nrecord()",
        "while x:\n    submit()\n    record()",
        # an optional recorder: with none there is nothing to record with
        "submit()\nif rec:\n    rec.record()",
        "submit()\nif rec is not None:\n    rec.record()",
        "submit()\nif rec is None:\n    return\nrec.record()",
        # not branches of control flow
        "submit()\nx and record()",
        "submit()\nwith x:\n    record()",
        "def done() -> None:\n    record()\nif x:\n    submit()\n    done()",
    ],
)
def test_every_finished_path_records_or_the_case_is_not_judged(repo: Path, body: str) -> None:
    entry = {"when_calls": "app.ports.submit", "must_also_call": ["app.ports.record", "*.record"]}
    write(repo, function(body))
    assert missing(run(repo, entry)) == []


def test_a_one_hop_record_counts_on_the_path_that_calls_it(repo: Path) -> None:
    helper = "def note() -> None:\n    record()\n\n\n"
    write(repo, HEADER + helper + "def go(x) -> None:\n    submit()\n    if x:\n        note()\n    else:\n        record()\n")
    assert missing(run(repo)) == []
    write(repo, HEADER + helper + "def go(x) -> None:\n    submit()\n    if x:\n        note()\n")
    assert missing(run(repo)) == [("app.domain", 9)]


def test_the_finding_names_the_action_line_and_where_the_path_leaves(repo: Path) -> None:
    write(repo, function("submit()\nif x:\n    return\nrecord()"))
    [violation] = [v for v in run(repo) if v.rule == Rule.MISSING_COMPANION_CALL]
    assert violation.line == 5 and "line 7" in violation.hint


@pytest.mark.parametrize(
    "body",
    [
        "def go(x) -> None:\n    submit()\n    if x == 0:\n        pass\n" + "    elif x == 1:\n        pass\n" * 1500
        + "    else:\n        pass\n    record()\n",
        "def go(x) -> None:\n    submit()\n" + "".join(f"    if x.a{i}:\n        x.go()\n    if x.a{i}:\n        x.go()\n" for i in range(60))
        + "    record()\n",
        "def go(x) -> None:\n    submit()\n" + "".join("    " * i + "try:\n" + "    " * (i + 1) + "x.go()\n" for i in range(1, 15))
        + "".join("    " * i + "finally:\n" + "    " * (i + 1) + "x.go()\n" for i in range(14, 0, -1)) + "    record()\n",
        "def go(x) -> None:\n    if x:\n        submit()\n    record(" + "a + " * 500 + "a)\n",
    ],
)
def test_a_very_large_function_neither_crashes_nor_stalls(repo: Path, body: str) -> None:
    write(repo, HEADER + body)
    started = time.monotonic()
    assert missing(run(repo)) == []
    assert time.monotonic() - started < 20


def test_the_flow_survives_the_scan_cache() -> None:
    facts = extract(function("submit()\nif x:\n    record()\nif x:\n    return\ntry:\n    y.go()\nexcept OSError:\n    pass"))
    assert facts.function_spans[0].flow
    assert cached_facts(["digest", facts_to_json(facts)]) == facts


def test_a_function_without_branches_keeps_no_flow() -> None:
    assert extract(function("submit()\nrecord()")).function_spans[0].flow == ()


@pytest.mark.parametrize(
    "body",
    [
        "async def go(x) -> None:\n    async with x:\n        submit()\n    async for item in x:\n        record()\n    if x:\n        return\n",
        "def go(x) -> None:\n    while True:\n        submit()\n        record()\n        if x:\n            return\n",
        "def go(x) -> None:\n    try:\n        x.prepare()\n    except OSError:\n        return\n    else:\n        submit()\n    finally:\n        x.close()\n    record()\n",
        "def go(x) -> None:\n    for item in x:\n        pass\n    else:\n        submit()\n    if x:\n        record()\n    else:\n        record()\n",
        "def go(x) -> None:\n    for item in x:\n        try:\n            submit()\n            if item:\n                break\n        finally:\n            record()\n",
    ],
)
def test_other_shapes_that_always_record_are_accepted(repo: Path, body: str) -> None:
    write(repo, HEADER + body)
    assert missing(run(repo)) == []


def test_a_pathlib_method_can_be_the_action_on_a_path(repo: Path) -> None:
    entry = {"when_calls": "pathlib.Path.unlink", "must_also_call": "app.ports.record"}
    head = "from pathlib import Path\nfrom app.ports import record\n\n\ndef go(p: Path, x) -> None:\n    p.unlink()\n"
    write(repo, head + "    if x:\n        record()\n")
    assert missing(run(repo, entry)) == [("app.domain", 6)]
    write(repo, head + "    if x:\n        record()\n    else:\n        record()\n")
    assert missing(run(repo, entry)) == []
