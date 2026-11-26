"""``[[construction]]`` (#234): which keywords a caller may or must pass to a class, and who may construct it at all."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.facts import extract
from arborescent.rules import check_modules
from arborescent.scan_cache_codec import facts_from_json, facts_to_json
from arborescent.selftest import plan

TYPE = "app.adapters.Progress"
HEAD = "from __future__ import annotations\n\n"
DEFINES = HEAD + "import dataclasses\n\n\n@dataclasses.dataclass\nclass Progress:\n    user: str\n    tier: int = 0\n    note: str = ''\n"
ENTRY: list[dict[str, Any]] = [{"type": TYPE, "forbid_kwargs": ["tier"], "reason": "tier is derived"}]
FORBIDDEN = Rule.CONSTRUCTION_FORBIDDEN_ARGUMENT
MISSING = Rule.CONSTRUCTION_MISSING_ARGUMENT
OUTSIDE = Rule.CONSTRUCTION_OUTSIDE_SITE
RULES = {FORBIDDEN, MISSING, OUTSIDE}
IMPORT = "from app.adapters import Progress\n"


def run(root: Path, construction: list[dict[str, Any]] | None = None) -> list[Violation]:
    policy = mini_policy(construction=ENTRY if construction is None else construction)
    scan = FsScanner().scan(policy, root)
    return [v for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index) if v.rule in RULES]


def project(root: Path, body: str, defines: str = DEFINES, **more: str) -> None:
    modules = {f"app/{name}.py": HEAD + text for name, text in {"domain": body, **more}.items()}
    write_files(root, {"app/__init__.py": "", "app/adapters.py": defines, **modules})


def hits(found: list[Violation], rule: Rule = FORBIDDEN) -> list[str]:
    return [v.subject for v in found if v.rule == rule]


@pytest.mark.parametrize(
    "body",
    [
        IMPORT + "X = Progress('u', tier=1)\n",
        "from app.adapters import Progress as P\nX = P('u', tier=1)\n",
        "import app.adapters\nX = app.adapters.Progress('u', tier=1)\n",
        "import app.adapters as a\nX = a.Progress('u', tier=1)\n",
        "from . import adapters\nX = adapters.Progress('u', tier=1)\n",
        "from .adapters import Progress\nX = Progress('u', tier=1)\n",
        IMPORT + "X = Progress('u', 1)\n",
        IMPORT + "X = Progress('u', 1, 'n')\n",
        IMPORT + "X = Progress(*('u', 1))\n",
        IMPORT + "X = Progress('u', 1, *rest)\n",
        IMPORT + "X = Progress(**{'tier': 1})\n",
        IMPORT + "X = Progress(**{**extra, 'tier': 1})\n",
        IMPORT + "X = Progress('u', **extra, tier=1)\n",
        IMPORT + "X = Progress[int]('u', tier=1)\n",
    ],
)
def test_a_forbidden_field_supplied_by_a_caller_is_reported(repo: Path, body: str) -> None:
    project(repo, body)
    [violation] = run(repo)
    assert (violation.rule, violation.subject) == (FORBIDDEN, "app.domain")
    assert "`tier`" in violation.detail and TYPE in violation.detail and "tier is derived" in violation.detail
    assert violation.line > 0


@pytest.mark.parametrize(
    "body",
    [
        IMPORT + "X = Progress('u')\n",
        IMPORT + "X = Progress(user='u', note='n')\n",
        IMPORT + "X = Progress(**data)\n",  # unknown keys: blind spot
        IMPORT + "X = Progress(*data)\n",  # unknown length: blind spot
        IMPORT + "X = Progress('u', *rest, 1)\n",  # after a splat the position is unknown
        IMPORT + "def f(x: Progress) -> int:\n    return x.tier\n",
        "class Progress:\n    pass\n\n\nX = Progress(tier=1)\n",
        "def f(progress: object) -> None:\n    progress.Progress(tier=1)\n",
    ],
)
def test_calls_that_do_not_certainly_supply_the_field_are_not_reported(repo: Path, body: str) -> None:
    project(repo, body)
    assert run(repo) == []


@pytest.mark.parametrize(
    "body",
    [
        "import dataclasses\n" + IMPORT + "def f(p: Progress) -> Progress:\n    return dataclasses.replace(p, tier=2)\n",
        "from dataclasses import replace as r\n" + IMPORT + "def f(p: 'Progress | None') -> object:\n    return r(p, tier=2)\n",
        "import dataclasses as dc\nfrom typing import Optional\n" + IMPORT + "def f(p: Optional[Progress]) -> object:\n    return dc.replace(p, **{'tier': 2})\n",
        "from copy import replace\n" + IMPORT + "def f() -> object:\n    p = Progress('u')\n    return replace(p, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "X = replace(Progress('u'), tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "P = Progress('u')\n\n\ndef f() -> object:\n    return replace(P, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: Progress) -> object:\n    p = replace(p, note='x')\n    return replace(p, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: Progress) -> object:\n    q = p\n    return replace(q, tier=2)\n",
        "from dataclasses import dataclass, replace\n" + IMPORT + "@dataclass\nclass H:\n    p: Progress\n\n    def f(self) -> object:\n        return replace(self.p, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "def outer(p: Progress) -> object:\n    def inner() -> object:\n        return replace(p, tier=2)\n\n    return inner\n",
    ],
)
def test_replace_on_a_value_known_to_be_the_type_is_reported(repo: Path, body: str) -> None:
    project(repo, body)
    [violation] = run(repo)
    assert violation.rule == FORBIDDEN and "replace" in violation.detail


@pytest.mark.parametrize(
    "body",
    [
        "from dataclasses import replace\n" + IMPORT + "def f(p) -> object:\n    return replace(p, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: int) -> object:\n    return replace(p, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "P = Progress('u')\n\n\ndef f(P: int) -> object:\n    return replace(P, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "def f() -> object:\n    x = x\n    return replace(x, tier=2)\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: Progress) -> object:\n    return replace(p, note='n')\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: Progress, c: object) -> object:\n    return replace(p, **c)\n",
        "from dataclasses import replace\n" + IMPORT + "def f(a: object) -> object:\n    return replace(*a, tier=2)\n",
        IMPORT + "def f(p: Progress) -> object:\n    return p.note.replace('a', 'b')\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: 'not an ( annotation') -> object:\n    return replace(p, tier=2)\n",
    ],
)
def test_replace_on_an_unknown_or_other_value_is_not_reported(repo: Path, body: str) -> None:
    project(repo, body)
    assert run(repo) == []


def test_a_method_replacing_its_own_instance_is_reported(repo: Path) -> None:
    defines = DEFINES + "\n    def bump(self) -> Progress:\n        return dataclasses.replace(self, tier=1)\n"
    project(repo, "", defines)
    assert hits(run(repo)) == ["app.adapters"]


def test_positional_arguments_are_mapped_through_inheritance_and_field_options(repo: Path) -> None:
    defines = HEAD + (
        "from dataclasses import KW_ONLY, dataclass, field\nfrom typing import ClassVar\n\n\n"
        "@dataclass\nclass Base:\n    user: str\n    hidden: int = field(init=False, default=0)\n\n\n"
        "@dataclass\nclass Progress(Base):\n    LIMIT: ClassVar[int] = 3\n    tier: int = 0\n    flag: int = field(default=0, kw_only=True)\n"
        "    _: KW_ONLY\n    note: str = ''\n"
    )
    entry = [{"type": TYPE, "forbid_kwargs": ["tier", "hidden", "note", "flag"]}]
    project(repo, IMPORT + "X = Progress('u', 2)\nY = Progress('u')\n", defines)
    [violation] = run(repo, entry)
    assert "`tier`" in violation.detail and "position" in violation.detail and violation.line == 4


@pytest.mark.parametrize(
    ("defines", "reported"),
    [
        ("class Progress:\n    def __init__(self, user, tier=0, *, note=''):\n        self.tier = tier\n", True),
        ("from typing import NamedTuple\n\n\nclass Progress(NamedTuple):\n    user: str\n    tier: int = 0\n", True),
        ("from dataclasses import dataclass\n\n\n@dataclass(kw_only=True)\nclass Progress:\n    user: str\n    tier: int = 0\n", False),
        ("from thirdparty import Model\n\n\nclass Progress(Model):\n    user: str\n    tier: int = 0\n", False),  # unknown base: blind spot
        ("import attrs\n\n\n@attrs.define\nclass Progress:\n    user: str\n    tier: int = 0\n", False),  # unknown decorator: blind spot
        ("class Progress(Progress):\n    pass\n", False),  # a self-referential base must not loop
    ],
)
def test_positional_mapping_by_class_kind(repo: Path, defines: str, reported: bool) -> None:
    project(repo, IMPORT + "X = Progress('u', 2)\n", HEAD + defines)
    assert (hits(run(repo)) == ["app.domain"]) is reported


def test_require_kwargs_must_be_passed_by_name(repo: Path) -> None:
    entry = [{"type": TYPE, "require_kwargs": ["note"]}]
    body = IMPORT + "A = Progress('u')\nB = Progress('u', 1, 'n')\nC = Progress('u', note='n')\nD = Progress('u', **extra)\n"
    project(repo, body)
    found = run(repo, entry)
    assert [(v.rule, v.line) for v in found] == [(MISSING, 4), (MISSING, 5)]
    assert "`note`" in found[0].detail


def test_replace_is_not_asked_for_required_keywords(repo: Path) -> None:
    project(repo, "from dataclasses import replace\n" + IMPORT + "def f(p: Progress) -> object:\n    return replace(p, tier=1)\n")
    assert run(repo, [{"type": TYPE, "require_kwargs": ["note"]}]) == []


def test_only_from_restricts_who_constructs_it_including_by_replace(repo: Path) -> None:
    entry = [{"type": TYPE, "only_from": ["app.main"], "reason": "one builder"}]
    body = "from dataclasses import replace\n" + IMPORT + "A = Progress('u')\nB = replace(A, note='n')\n"
    project(repo, body, main=IMPORT + "A = Progress('u')\n")
    found = run(repo, entry)
    assert [(v.rule, v.subject, v.line) for v in found] == [(OUTSIDE, "app.domain", 5), (OUTSIDE, "app.domain", 6)]
    assert "app.main" in found[0].detail and "one builder" in found[0].detail


@pytest.mark.parametrize(
    ("init", "imports", "entry_type"),
    [
        ("from .adapters import Progress\n", "from app import Progress\n", TYPE),
        ("from app.adapters import Progress\n", "import app\nfrom app import Progress\n", TYPE),
        ("from .adapters import Progress as P\n", "from app import P as Progress\n", TYPE),
        ("from .adapters import Progress\n", IMPORT, "app.Progress"),  # the entry may name the re-exported path
    ],
)
def test_a_reexported_name_resolves_to_the_class(repo: Path, init: str, imports: str, entry_type: str) -> None:
    project(repo, imports + "X = Progress('u', 2)\n")
    write_files(repo, {"app/__init__.py": init})
    assert hits(run(repo, [{"type": entry_type, "forbid_kwargs": ["tier"]}])) == ["app.domain"]


MARKED = HEAD + (
    "from dataclasses import dataclass, field, replace\n\n\n@dataclass\nclass Progress:\n    user: str\n"
    "    tier: int = field(default=0, metadata={'arbor': 'derived'})\n"
)


@pytest.mark.parametrize(
    "body",
    [
        IMPORT + "X = Progress('u', tier=1)\n",
        IMPORT + "X = Progress('u', 1)\n",
        "from dataclasses import replace\n" + IMPORT + "def f(p: Progress) -> object:\n    return replace(p, tier=1)\n",
        IMPORT + "class Sub(Progress):\n    pass\n\n\nX = Sub('u', tier=1)\n",
    ],
)
def test_the_field_marker_works_without_a_policy_entry(repo: Path, body: str) -> None:
    project(repo, body, MARKED)
    [violation] = run(repo, [])
    assert violation.rule == FORBIDDEN and "derived" in violation.detail and "`tier`" in violation.detail


def test_a_marked_field_with_a_matching_entry_is_reported_once(repo: Path) -> None:
    project(repo, IMPORT + "X = Progress('u', tier=1)\n", MARKED)
    assert len(run(repo)) == 1


def test_an_unrelated_metadata_value_is_no_marker(repo: Path) -> None:
    project(repo, IMPORT + "X = Progress('u', tier=1)\n", MARKED.replace("'derived'", "'other'"))
    assert run(repo, []) == []


def test_stale_entries_are_reported(repo: Path) -> None:
    project(repo, "")
    entries = [
        {"type": "app.gone.Progress", "forbid_kwargs": ["tier"]},
        {"type": "app.adapters.Gone", "require_kwargs": ["tier"]},
        {"type": TYPE, "forbid_kwargs": ["teir"]},
        {"type": TYPE, "only_from": ["app.nowhere*"]},
    ]
    found = run(repo, entries)
    assert all(v.line == 0 for v in found) and len(found) == 4
    assert sorted(v.rule for v in found) == sorted([FORBIDDEN, MISSING, FORBIDDEN, OUTSIDE])
    text = "\n".join(v.detail for v in found)
    assert "app.gone" in text and "Gone" in text and "`teir`" in text and "app.nowhere*" in text


def test_field_names_are_not_called_stale_when_the_class_cannot_be_read_fully(repo: Path) -> None:
    project(repo, "", HEAD + "from thirdparty import Model\n\n\nclass Progress(Model):\n    pass\n")
    assert run(repo) == []


def test_a_violation_is_a_baseline_entry_like_any_other(repo: Path) -> None:
    project(repo, IMPORT + "X = Progress('u', tier=1)\n")
    [violation] = run(repo)
    assert violation.key.startswith("construction-forbidden-argument|app.domain|")


@pytest.mark.parametrize(
    "value",
    [
        "x", [{}], [{"forbid_kwargs": ["a"]}], [{"type": "Progress", "forbid_kwargs": ["a"]}], [{"type": TYPE}],
        [{"type": TYPE, "forbid_kwargs": []}], [{"type": TYPE, "only_from": []}], [{"type": TYPE, "forbid_kwargs": "tier"}],
        [{"type": TYPE, "forbid_kwargs": [""]}], [{"type": TYPE, "forbid_kwargs": [" "]}], [{"type": TYPE, "forbid_kwargs": ["a\x00"]}],
        [{"type": TYPE, "require_kwargs": [1]}], [{"type": TYPE, "only_from": [""]}], [{"type": TYPE, "forbid_kwarg": ["a"]}],
        [{"type": TYPE, "forbid_kwargs": ["a"], "require_kwargs": ["a"]}], [{"type": TYPE, "forbid_kwargs": ["a"], "reason": 1}],
        [{"type": TYPE + "\x00", "forbid_kwargs": ["a"]}],
    ],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="construction"):
        policy_from_mapping({**MINI, "construction": value})


def test_it_is_off_by_default_and_a_scoped_policy_rejects_a_root_entry(repo: Path) -> None:
    assert mini_policy().construction == ()
    project(repo, IMPORT + "X = Progress('u', tier=1)\n")
    assert run(repo, []) == []
    with pytest.raises(PolicyError, match="construction"):
        policy_from_mapping({"scopes": {"a": {}}, "construction": ENTRY})


def test_call_and_class_facts_survive_the_scan_cache() -> None:
    source = (
        "from dataclasses import dataclass, field, replace\nfrom x import C\n\n\n@dataclass\nclass D(C):\n"
        "    a: int = field(default=0, metadata={'arbor': 'derived'})\n\n\nclass E:\n    def __init__(self, a, **k):\n        pass\n\n\n"
        "C(1, *x, b=2, **y)\n\n\ndef f(d: D) -> None:\n    replace(d, a=1)\n"
    )
    facts = extract(source)
    assert facts.call_shapes and facts.class_shapes
    assert facts_from_json(facts_to_json(facts)) == facts


def test_selftest_probes_each_declared_restriction_and_marker(repo: Path) -> None:
    project(repo, "", MARKED)
    write_files(repo, {"app/main.py": ""})
    entry: list[dict[str, Any]] = [{"type": TYPE, "require_kwargs": ["user"], "only_from": ["app.main"]}]
    policy = mini_policy(construction=entry)
    mutations = [m for m in plan(policy, FsScanner().scan(policy, repo))[0] if m.rule in RULES]
    assert sorted(m.rule for m in mutations) == sorted([OUTSIDE, MISSING, FORBIDDEN])
    for mutation in mutations:  # every probe makes its rule newly fire
        before = {v.key for v in run(repo, entry)}
        path = repo / mutation.file
        original = None if mutation.new_file else path.read_text(encoding="utf-8")
        _ = path.write_text(mutation.content if original is None else f"{original}\n{mutation.append}\n", encoding="utf-8")
        after = run(repo, entry)
        if original is None:
            path.unlink()
        else:
            _ = path.write_text(original, encoding="utf-8")
        assert any(v.rule == mutation.rule and v.key not in before for v in after), mutation.label


def test_a_reexport_named_like_its_submodule_does_not_loop(repo: Path) -> None:
    project(repo, "from app import helper\nX = helper()\n", helper="\n\ndef helper() -> None:\n    pass\n")
    write_files(repo, {"app/__init__.py": "from app.helper import helper\n"})
    assert run(repo) == []


@pytest.mark.parametrize(
    "defines",
    [
        "class Progress:\n    def __new__(cls, user, tier=0):\n        return super().__new__(cls)\n",
        "class Progress:\n    def __new__(cls, **kwargs):\n        return super().__new__(cls)\n",
    ],
)
def test_a_constructor_defined_by_new_is_unreadable_not_stale(repo: Path, defines: str) -> None:
    project(repo, IMPORT + "X = Progress('u', 2)\n", HEAD + defines)
    assert run(repo) == []


def test_the_implementation_of_an_overloaded_init_is_the_one_read(repo: Path) -> None:
    defines = HEAD + (
        "from typing import overload\n\n\nclass Progress:\n    @overload\n    def __init__(self, user) -> None: ...\n"
        "    @overload\n    def __init__(self, user, extra) -> None: ...\n    def __init__(self, user, tier=0) -> None:\n        pass\n"
    )
    project(repo, IMPORT + "X = Progress('u', 2)\n", defines)
    assert hits(run(repo)) == ["app.domain"]


def test_a_long_alias_chain_does_not_exhaust_the_stack() -> None:
    chain = "".join(f"    x{i + 1} = x{i}\n" for i in range(3000))
    source = "from dataclasses import replace\n\n\ndef f(x0: int) -> None:\n" + chain + "    replace(x3000, a=1)\n"
    assert extract(source).call_shapes[-1].callee == "dataclasses.replace"


def test_probes_do_not_collide_with_an_already_recorded_violation(repo: Path) -> None:
    project(repo, "", aaa=IMPORT + "X = Progress('u', tier=None)\n")
    policy = mini_policy(construction=ENTRY)
    [probe] = [m for m in plan(policy, FsScanner().scan(policy, repo))[0] if m.rule == FORBIDDEN]
    assert probe.new_file and not (repo / probe.file).exists()
