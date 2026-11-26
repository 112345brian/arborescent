"""#233 ``effect-seam-bypassed``: a function or class given an injected clock, environment or generator that also uses the real one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, services, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule
from arborescent.engine import run_own
from arborescent.facts import extract
from arborescent.rules import RuleContext, check_modules, fits_ring
from arborescent.scan_cache_codec import CACHE_VERSION, facts_from_json, facts_to_json
from arborescent.selftest import plan, selftest

OPEN = {domain: {"may_import": ["domain"] if domain == "ports" else [], "external": ["*"], "forbid": []} for domain in ("domain", "ports")}


def policy_for(**table: Any) -> Any:
    return mini_policy(ring=ring_with(**OPEN), effect_seam_bypassed=table)


def findings(root: Path, source: str, **table: Any) -> list[Any]:
    write_files(root, {"app/ports.py": source, "app/adapters.py": "X = 1\n", "app/main.py": "X = 1\n"})
    policy = policy_for(**table)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [v for v in found if v.rule == Rule.EFFECT_SEAM_BYPASSED]


def detail(function: str, seam: str, category: str) -> str:
    return f"[ports] {function}() takes the effect seam '{seam}' but also uses the real {category}; use '{seam}' instead"


@pytest.mark.parametrize(
    ("source", "function", "seam", "category"),
    [
        ("import time\ndef f(now):\n    return now(), time.strftime('%Y', time.localtime())\n", "f", "now", "clock"),
        ("from datetime import datetime\ndef f(*, clock):\n    return datetime.now()\n", "f", "clock", "clock"),
        ("import time\ndef f(clock_now, /):\n    return time.time()\n", "f", "clock_now", "clock"),
        ("import os\ndef f(environ):\n    return os.environ['A']\n", "f", "environ", "env"),
        ("import os\ndef f(environ):\n    return os.getenv('A')\n", "f", "environ", "env"),
        ("import random\ndef f(rng):\n    return random.random()\n", "f", "rng", "randomness"),
        ("import uuid\ndef f(uuid_factory):\n    return uuid.uuid4()\n", "f", "uuid_factory", "identity"),
        # a parameter whose default is an effect function (#101), whatever it is called
        ("import uuid\ndef f(new_id=uuid.uuid4):\n    return uuid.uuid4()\n", "f", "new_id", "identity"),
        ("import time\ndef f(pause=time.sleep):\n    time.sleep(1)\n", "f", "pause", "sleep"),
        ("from datetime import datetime\ndef f(stamp=lambda: datetime.now()):\n    return datetime.now()\n", "f", "stamp", "clock"),
        # a parameter whose annotation names the seam
        ("import random\ndef f(source: random.Random):\n    return random.random()\n", "f", "source", "randomness"),
        ("import random\ndef f(source: 'random.Random | None' = None):\n    return random.random()\n", "f", "source", "randomness"),
        ("import time\nfrom app import domain\ndef f(ticker: domain.Clock):\n    return time.time()\n", "f", "ticker", "clock"),
        # closures and lambdas defined inside
        ("import time\ndef f(now):\n    def later():\n        return time.time()\n    return later\n", "f", "now", "clock"),
        ("import time\ndef f(now):\n    return lambda: time.time()\n", "f", "now", "clock"),
        ("import time\ndef f(now):\n    def inner(at=time.time()):\n        return at\n    return inner\n", "f", "now", "clock"),
        ("import time\ndef outer():\n    def f(now):\n        return time.time()\n    return f\n", "outer.f", "now", "clock"),
        ("import time\nasync def f(now):\n    return time.time()\n", "f", "now", "clock"),
        # the real function handed on in place of the injected one
        ("import os\ndef g(e):\n    pass\ndef f(environ):\n    g(os.environ)\n", "f", "environ", "env"),
    ],
)
def test_a_bypassed_seam_is_reported(repo: Path, source: str, function: str, seam: str, category: str) -> None:
    assert [(v.subject, v.detail) for v in findings(repo, source)] == [("app.ports", detail(function, seam, category))], source


@pytest.mark.parametrize(
    "source",
    [
        "",
        "import time\ndef f():\n    return time.time()\n",  # no seam
        "import time\ndef f(now):\n    return now()\n",
        "import os\ndef f(now):\n    return os.getenv('A')\n",  # another category
        "import time\ndef f(clock=time.monotonic):\n    return clock(), time.time()\n",  # the default says monotonic, not wall clock
        "import time\ndef f(now=time.time):\n    return now()\n",
        "import random\ndef f(rng):\n    return random.Random(3).random() + rng.random()\n",  # seeded: deterministic
        "import random\ndef f(rng):\n    return isinstance(rng, random.Random)\n",
        "import time\ndef f(now: float) -> None:\n    x: time.struct_time\n",
        # the parameter shadows an imported name of the same spelling
        "from os import environ\ndef f(environ):\n    return environ['A']\n",
        "from time import time as now\ndef f(now):\n    return now()\n",
        # the real effect as the fallback for a seam that was not injected
        "import time\ndef f(now=None):\n    now = now or time.time\n    return now()\n",
        "from datetime import datetime\ndef f(now=None):\n    return now or datetime.now()\n",
        "from datetime import datetime\ndef f(now=None):\n    return datetime.now() if now is None else now\n",
        "from datetime import datetime\ndef f(now=None):\n    return now if now is not None else datetime.now()\n",
        "from datetime import datetime\ndef f(now=None):\n    return now() if now else datetime.now()\n",
        "import os\ndef f(environ=None):\n    if environ is None:\n        environ = os.environ\n    return environ\n",
        "import os\ndef f(environ=None):\n    if not environ:\n        environ = os.environ\n    return environ\n",
        "import time\ndef f(now=None):\n    if now is not None:\n        return now\n    return time.time()\n",
        "import time\ndef f(now=None):\n    if now:\n        pass\n    else:\n        return time.time()\n",
        # a closure with its own seam is judged by itself
        "def outer(now):\n    def inner(now):\n        return now()\n    return inner\n",
        "import time\ndef f(*now, **clock):\n    return time.time()\n",
        "import time\nclass C:\n    def __init__(self):\n        self.at = time.time()\n",
        "import time\ndef now():\n    return time.time()\n",
        "import time\nclass C:\n    now: float\n    def m(self):\n        return time.time()\n",  # not a dataclass: no field is injected
    ],
)
def test_what_does_not_bypass_a_seam_is_not_reported(repo: Path, source: str) -> None:
    assert findings(repo, source) == [], source


def test_a_class_seam_covers_its_methods_and_their_closures(repo: Path) -> None:
    source = (
        "import time\nfrom datetime import datetime\n"
        "class C:\n"
        "    def __init__(self, now=None):\n        self._now = now or datetime.now\n        self.started = time.time()\n"
        "    def stamp(self):\n        return datetime.now()\n"
        "    def later(self):\n        return lambda: time.time()\n"
        "    def honest(self):\n        return self._now() if self._now else datetime.now()\n"
        "    @staticmethod\n    def static():\n        return time.time()\n"
        "    @classmethod\n    def build(cls):\n        return time.time()\n"
    )

    def injected(method: str) -> str:
        return f"[ports] C.{method}() has the effect seam 'now' injected into C but also uses the real clock; use 'now' instead"

    expected = [detail("C.__init__", "now", "clock"), injected("stamp"), injected("later")]
    assert sorted(v.detail for v in findings(repo, source)) == sorted(expected)


def test_dataclass_fields_are_class_seams(repo: Path) -> None:
    source = (
        "import time\nfrom collections.abc import Callable\nfrom dataclasses import dataclass\n"
        "@dataclass(frozen=True)\nclass C:\n    now: Callable[[], float] = time.time\n    def m(self):\n        return time.time()\n"
    )
    assert len(findings(repo, source)) == 1


def test_one_finding_per_function_and_category_names_every_seam(repo: Path) -> None:
    source = "import os, time\ndef f(now, clock, environ):\n    time.time()\n    time.time_ns()\n    return os.getenv('A')\ndef g(now):\n    return time.time()\n"
    found = findings(repo, source)
    assert sorted(v.detail for v in found) == sorted([
        "[ports] f() takes the effect seams 'clock', 'now' but also uses the real clock; use 'clock' or 'now' instead",
        detail("f", "environ", "env"),
        detail("g", "now", "clock"),
    ])
    clock = next(v for v in found if "f() takes the effect seams" in v.detail)
    assert clock.line == 3 and clock.hint == " (it uses time.time, time.time_ns)"
    assert baseline.in_family(clock.key, "effects") and baseline.in_family(clock.key, "effect-seam-bypassed")


def test_the_rule_is_off_without_its_table(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import time\ndef f(now):\n    return time.time()\n"})
    policy = mini_policy(ring=ring_with(**OPEN))
    scan = FsScanner().scan(policy, repo)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    assert [v for v in found if v.rule == Rule.EFFECT_SEAM_BYPASSED] == []


def test_rings_parameters_and_annotations_are_configurable(repo: Path) -> None:
    source = "import time\ndef f(today):\n    return time.time()\ndef g(now):\n    return time.time()\ndef h(t: 'Ticker'):\n    return time.time()\n"
    assert findings(repo, source, rings=["domain"]) == []
    assert [v.detail for v in findings(repo, source, parameters={"today": "clock"})] == [detail("f", "today", "clock")]
    assert [v.detail for v in findings(repo, source, parameters={}, annotations={"Ticker": "clock"})] == [detail("h", "t", "clock")]


@pytest.mark.parametrize(
    "table",
    [
        True, {"ring": ["domain"]}, {"rings": "domain"}, {"rings": [""]}, {"parameters": ["now"]},
        {"parameters": {"now": "time"}}, {"parameters": {"": "clock"}}, {"parameters": {"now": 1}}, {"annotations": {"X": "filesystem"}},
    ],
)
def test_the_table_is_validated(table: object) -> None:
    with pytest.raises(PolicyError, match="effect_seam_bypassed"):
        mini_policy(effect_seam_bypassed=table)


def test_the_table_is_set_per_scope() -> None:
    scope = {"sources": ["a"], "source_roots": ["."], "rings": ["domain"], "effect_seam_bypassed": {}}
    policy = policy_from_mapping({"scopes": {"a": scope}})
    assert policy.scope("a").policy.effect_seam_bypassed is not None


def test_seam_facts_survive_the_cache_round_trip() -> None:
    facts = extract("import time\nclass C:\n    def __init__(self, now):\n        pass\n    def m(self, x: int = 1):\n        return now or time.time()\n")
    assert facts.seams.frames and facts.seams.uses
    assert facts_from_json(json.loads(json.dumps(facts_to_json(facts)))) == facts


def test_a_cache_written_before_seam_facts_is_parsed_again(repo: Path, tmp_path: Path) -> None:
    write_files(repo, {"app/ports.py": "import time\ndef f(now):\n    return time.time()\n"})
    policy = policy_for()
    cache = tmp_path / "cache"
    run_own(policy, repo, services(cache=cache), practices=False)
    document = json.loads((cache / "scan.json").read_text(encoding="utf-8"))
    assert document["v"] == CACHE_VERSION >= 27
    document["v"] = 26
    for record in document["files"].values():
        record[1] = record[1][:28]
    (cache / "scan.json").write_text(json.dumps(document), encoding="utf-8")
    found, scan = run_own(policy, repo, services(cache=cache), practices=False)
    assert scan.parsed == len(scan.modules)
    assert [v.subject for v in found if v.rule == Rule.EFFECT_SEAM_BYPASSED] == ["app.ports"]


def test_deeply_nested_and_large_sources_do_not_overflow_or_stall() -> None:
    chain = " + ".join(["time.time()"] * 400)
    assert extract(f"import time\ndef f(now):\n    return {chain}\n").seams.uses
    many = "import time\n" + "".join(f"def f{i}(now=None):\n    if now:\n        pass\n    return time.time()\n" for i in range(2000))
    assert len(extract(many).seams.frames) == 2000


def test_fits_ring_counts_a_bypassed_seam(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import time\ndef f(now):\n    return time.time()\n"})
    policy = policy_for(rings=["ports"])
    scan = FsScanner().scan(policy, repo)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    info = scan.modules["app.domain"]
    assert fits_ring(context, "app.domain", info, "domain", scan.known)
    assert not fits_ring(context, "app.domain", info, "ports", scan.known)


def test_selftest_probes_the_rule_when_it_is_enabled(repo: Path) -> None:
    policy = policy_for()
    scan = FsScanner().scan(policy, repo)
    assert Rule.EFFECT_SEAM_BYPASSED in {m.rule for m in plan(policy, scan)[0]}
    report = selftest(policy, repo, services())
    assert any(item.startswith("[effect-seam-bypassed]") for item in report.killed)
    assert not [item for item in report.survived if "effect-seam-bypassed" in item]
