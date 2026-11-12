"""``mixed-purity`` (#173): a module with substantial pure logic next to effectful code, in any ring."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, services, write_files

from arborescent import baseline as ratchet
from arborescent.adapters import FsScanner
from arborescent.cli_purity import run_purity
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.engine import blocking, run_own
from arborescent.facts import ModuleInfo, extract
from arborescent.purity import assess
from arborescent.rules import check_modules
from arborescent.selftest import plan, selftest

# The shape of the consumer's `bundle_schema/hydration.py`: parse and dump, an in-memory memo, a process-global slot written
# by a bind function, pure validation, filesystem walking and logging, all in one module.
HYDRATION = '''\
from __future__ import annotations

import json
import logging
import os

_MEMO: dict[str, dict[str, object]] = {}
_SLOT: object | None = None
LOG = logging.getLogger(__name__)


def parse(text: str) -> dict[str, object]:
    data = json.loads(text)
    out: dict[str, object] = {}
    for key, value in data.items():
        out[str(key)] = value
    return out


def dump(data: dict[str, object]) -> str:
    items = sorted(data.items())
    body = {key: value for key, value in items}
    return json.dumps(body, indent=2)


def validate(data: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for key, value in data.items():
        if value is None:
            errors.append(key)
    return errors


def migrate(data: dict[str, object]) -> dict[str, object]:
    out = dict(data)
    out.setdefault("version", 1)
    out["migrated"] = True
    return out


def hydrate(key: str, text: str) -> dict[str, object]:
    if key in _MEMO:
        return _MEMO[key]
    _MEMO[key] = parse(text)
    return _MEMO[key]


def bind(value: object) -> None:
    global _SLOT
    _SLOT = value


def walk(root: str) -> list[str]:
    LOG.info("walking %s", root)
    return [name for name in os.listdir(root) if name.endswith(".json")]
'''

PURE_ONLY = "from __future__ import annotations\n\n" + "".join(
    f"def f{i}(x: int) -> int:\n    y = x + {i}\n    z = y * 2\n    return z - 1\n\n\n" for i in range(4)
)

EFFECT_ONLY = '''\
from __future__ import annotations

import os


def read(path: str) -> str:
    with open(path) as handle:
        return handle.read()


def listing(path: str) -> list[str]:
    return os.listdir(path)


def write(path: str, text: str) -> None:
    with open(path, "w") as handle:
        handle.write(text)
'''

RINGS = ring_with(**{name: {**ring, "external": ["*"]} for name, ring in ring_with().items()})


def pure_function(name: str, lines: int = 4) -> str:
    body = "".join(f"    v{i} = x + {i}\n" for i in range(lines - 1))
    return f"def {name}(x: int) -> int:\n{body}    return v0\n\n\n"


def run(root: Path, files: dict[str, str], **extra: Any) -> list[Violation]:
    shutil.rmtree(root / "app", ignore_errors=True)
    write_files(root, {"app/__init__.py": "", **files})
    policy = mini_policy(ring=RINGS, **extra)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [v for v in found if v.rule == Rule.MIXED_PURITY]


def only(files: dict[str, str]) -> dict[str, str]:
    return {f"app/{name}.py": text for name, text in files.items()}


@pytest.mark.parametrize("ring_module", ["domain", "ports", "adapters", "main"])
def test_hydration_shape_is_flagged_in_every_ring(repo: Path, ring_module: str) -> None:
    found = run(repo, {f"app/{ring_module}.py": HYDRATION})
    assert [v.subject for v in found] == [f"app.{ring_module}"]


def test_the_report_names_kinds_both_sides_and_the_blast_radius(repo: Path) -> None:
    found = run(repo, {"app/domain.py": HYDRATION, "app/ports.py": "from . import domain\n", "app/main.py": "from . import domain\n"})
    assert len(found) == 1
    hint = found[0].hint
    for kind in ("filesystem", "logging", "global", "memo"):
        assert kind in hint
    assert "bind" in hint and "walk" in hint and "hydrate" in hint  # the effectful definitions
    assert "parse" in hint and "validate" in hint  # the pure ones
    assert "imported by 2 modules" in hint
    assert found[0].detail == "[domain] mixes substantial pure logic with effectful code"  # no counts or names: a stable baseline key
    assert found[0].line > 0


def test_pure_only_and_effect_only_modules_are_not_mixed(repo: Path) -> None:
    assert run(repo, {"app/domain.py": PURE_ONLY, "app/adapters.py": EFFECT_ONLY}) == []


def test_an_empty_module_is_not_mixed(repo: Path) -> None:
    assert run(repo, {"app/domain.py": ""}) == []


def test_threshold_boundary_of_pure_units(repo: Path) -> None:
    effect = "def go() -> str:\n    return input()\n"
    one = "from __future__ import annotations\n\n" + pure_function("a") + effect
    two = "from __future__ import annotations\n\n" + pure_function("a") + pure_function("b") + effect
    assert run(repo, {"app/domain.py": one}) == []
    assert [v.subject for v in run(repo, {"app/domain.py": two})] == ["app.domain"]


def test_threshold_boundary_of_body_lines(repo: Path) -> None:
    effect = "def go() -> str:\n    return input()\n"
    short = "".join(pure_function(n, 3) for n in "ab") + effect
    exact = "".join(pure_function(n, 4) for n in "ab") + effect
    assert run(repo, {"app/domain.py": short}) == []
    assert len(run(repo, {"app/domain.py": exact})) == 1


def test_thresholds_are_configurable(repo: Path) -> None:
    effect = "def go() -> str:\n    return input()\n"
    one = pure_function("a", 3) + effect
    assert run(repo, {"app/domain.py": one}) == []
    assert len(run(repo, {"app/domain.py": one}, mixed_purity={"min_pure_units": 1, "min_body_lines": 3})) == 1
    assert run(repo, {"app/domain.py": HYDRATION}, mixed_purity={"min_pure_units": 9}) == []


def test_the_rule_can_be_turned_off(repo: Path) -> None:
    assert run(repo, {"app/domain.py": HYDRATION}, mixed_purity={"enabled": False}) == []


def test_a_function_calling_an_effectful_sibling_is_not_pure(repo: Path) -> None:
    code = (
        "def effect() -> str:\n    return input()\n\n\n"
        + "".join(f"def wrap{i}(x: int) -> int:\n    a = effect()\n    b = x\n    c = len(a)\n    return b + c\n\n\n" for i in range(3))
    )
    assert run(repo, {"app/domain.py": code}) == []


def test_forwarding_wrappers_and_models_do_not_count_as_pure_logic(repo: Path) -> None:
    code = (
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\nclass Model:\n    a: int\n    b: int\n    c: int\n    d: int\n\n\n"
        "@dataclass\nclass Other:\n    a: int\n    b: int\n    c: int\n    d: int\n\n\n"
        "def go() -> str:\n    return input()\n"
    )
    assert run(repo, {"app/domain.py": code}) == []


def test_a_class_with_real_methods_is_pure_logic(repo: Path) -> None:
    cls = "".join(
        f"class C{i}:\n    def m(self, x: int) -> int:\n        a = x + 1\n        b = a * 2\n        return b\n\n\n" for i in range(2)
    )
    assert len(run(repo, {"app/domain.py": cls + "def go() -> str:\n    return input()\n"})) == 1


def test_import_time_effect_counts(repo: Path) -> None:
    code = "import os\n\nHOME = os.environ.get('HOME')\n\n\n" + pure_function("a") + pure_function("b")
    found = run(repo, {"app/domain.py": code})
    assert len(found) == 1 and "<import time>" in found[0].hint


def test_the_main_guard_block_is_not_an_import_time_effect(repo: Path) -> None:
    code = pure_function("a") + pure_function("b") + "if __name__ == '__main__':\n    print(a(1))\n"
    assert run(repo, {"app/domain.py": code}) == []


def test_type_checking_imports_are_not_effects(repo: Path) -> None:
    code = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import subprocess\n    import logging\n\n\n" + pure_function("a") + pure_function("b")
    assert run(repo, {"app/domain.py": code}) == []


def test_parameter_default_seams_are_not_effects(repo: Path) -> None:
    code = "import time\n\n\ndef stamp(now=time.time) -> float:\n    return now()\n\n\n" + pure_function("a") + pure_function("b")
    assert run(repo, {"app/domain.py": code}) == []


@pytest.mark.parametrize(
    ("effect", "kind"),
    [
        ("import logging\nlog = logging.getLogger('x')\n\n\ndef go() -> None:\n    log.info('x')\n", "logging"),
        ("import logging\n\n\ndef go() -> None:\n    logging.warning('x')\n", "logging"),
        ("from logging import info\n\n\ndef go() -> None:\n    info('x')\n", "logging"),
        ("_CACHE: dict[int, int] = {}\n\n\ndef go(x: int) -> int:\n    _CACHE[x] = x\n    return x\n", "memo"),
        ("_REGISTRY: list[int] = []\n\n\ndef go(x: int) -> None:\n    _REGISTRY.append(x)\n", "state"),
        ("_SLOT = None\n\n\ndef go(x: int) -> None:\n    global _SLOT\n    _SLOT = x\n", "global"),
    ],
)
def test_each_added_effect_kind_makes_a_module_mixed(repo: Path, effect: str, kind: str) -> None:
    found = run(repo, {"app/domain.py": "from __future__ import annotations\n\n" + pure_function("a") + pure_function("b") + effect})
    assert len(found) == 1 and kind in found[0].hint


def test_a_local_that_shadows_a_module_container_is_not_state(repo: Path) -> None:
    code = "_ITEMS: list[int] = []\n\n\ndef go(x: int) -> list[int]:\n    _ITEMS = []\n    _ITEMS.append(x)\n    return _ITEMS\n"
    assert run(repo, {"app/domain.py": pure_function("a") + pure_function("b") + code}) == []


def test_reading_module_state_is_not_an_effect(repo: Path) -> None:
    code = "_TABLE = {'a': 1}\n\n\ndef look(k: str) -> int:\n    return _TABLE[k]\n"
    assert run(repo, {"app/domain.py": pure_function("a") + pure_function("b") + code}) == []


def test_ambient_lookups_are_found_through_configured_patterns(repo: Path) -> None:
    files = {
        "app/ports.py": "def state_root() -> str:\n    return '.'\n",
        "app/domain.py": (
            "from .ports import state_root\n\n\n" + pure_function("a") + pure_function("b")
            + "def where() -> str:\n    return state_root()\n"
        ),
    }
    assert run(repo, files) == []
    found = run(repo, files, mixed_purity={"ambient": ["app.ports.state_root"]})
    assert len(found) == 1 and "ambient" in found[0].hint


def test_kinds_are_configurable(repo: Path) -> None:
    code = pure_function("a") + pure_function("b") + "import logging\n\n\ndef go() -> None:\n    logging.info('x')\n"
    assert len(run(repo, {"app/domain.py": code})) == 1
    assert run(repo, {"app/domain.py": code}, mixed_purity={"kinds": ["filesystem"]}) == []


def test_unknown_kinds_and_keys_are_rejected() -> None:
    with pytest.raises(PolicyError, match="unknown effect kind"):
        mini_policy(mixed_purity={"kinds": ["telepathy"]})
    with pytest.raises(PolicyError, match="unknown key"):
        mini_policy(mixed_purity={"min_units": 2})
    with pytest.raises(PolicyError, match="positive integer"):
        mini_policy(mixed_purity={"min_pure_units": 0})


def allow(*entries: dict[str, str]) -> dict[str, Any]:
    return {"allow": list(entries)}


def test_an_allowlisted_module_is_not_reported(repo: Path) -> None:
    found = run(repo, {"app/domain.py": HYDRATION}, mixed_purity=allow({"module": "app.domain", "reason": "hydration is one unit until the split lands"}))
    assert found == []


def test_an_allow_entry_needs_a_reason() -> None:
    for entry in ({"module": "app.domain"}, {"module": "app.domain", "reason": ""}, {"module": "app.domain", "reason": "   "}):
        with pytest.raises(PolicyError, match="reason"):
            mini_policy(mixed_purity=allow(entry))
    with pytest.raises(PolicyError, match="module"):
        mini_policy(mixed_purity=allow({"reason": "why"}))


def test_a_stale_allow_entry_fails(repo: Path) -> None:
    gone = run(repo, {"app/domain.py": HYDRATION}, mixed_purity=allow({"module": "app.vanished", "reason": "was split"}))
    assert [v.subject for v in gone] == ["app.domain", "app.vanished"]
    assert "matches no module" in gone[1].detail
    clean = run(repo, {"app/domain.py": PURE_ONLY}, mixed_purity=allow({"module": "app.domain", "reason": "was mixed once"}))
    assert [v.subject for v in clean] == ["app.domain"] and "no module it matches is mixed any more" in clean[0].detail


def test_allow_entries_match_prefixes_and_globs(repo: Path) -> None:
    files = {"app/domain.py": HYDRATION, "app/adapters.py": HYDRATION}
    entries = allow({"module": "app.dom*", "reason": "r1"}, {"module": "app.adapters", "reason": "r2"})
    assert run(repo, files, mixed_purity=entries) == []


def test_baseline_interplay_the_key_is_stable_and_shrinks(repo: Path) -> None:
    first = run(repo, {"app/domain.py": HYDRATION})
    renamed = run(repo, {"app/domain.py": HYDRATION.replace("def migrate", "def migrate2")})
    assert [v.key for v in first] == [v.key for v in renamed]  # renaming a function does not orphan the baselined key
    recorded = {v.key for v in first}
    held = ratchet.apply(first, recorded, complete=True)
    assert held.new == [] and not held.stale
    shrunk = ratchet.apply(run(repo, {"app/domain.py": PURE_ONLY}), recorded, complete=True)
    assert shrunk.new == [] and set(shrunk.stale) == recorded  # a fixed module leaves a stale entry the ratchet makes you delete
    grown = ratchet.apply(run(repo, {"app/domain.py": HYDRATION, "app/adapters.py": HYDRATION}), recorded, complete=True)
    assert [v.subject for v in grown.new] == ["app.adapters"]


def test_extraction_attributes_spans_and_kinds() -> None:
    facts = extract(HYDRATION)
    units = {u.name: u for u in facts.purity.units}
    assert set(units) == {"parse", "dump", "validate", "migrate", "hydrate", "bind", "walk"}
    assert units["parse"].body_lines == 5 and units["parse"].logic
    assert [kind for _, kind in units["walk"].kinds] == ["logging"]
    assert {kind for _, kind in units["hydrate"].kinds} == {"memo"}
    assert units["hydrate"].calls == ("parse",)



def test_mixed_modules_under_an_excluded_path_are_reported_as_notes(repo: Path, tmp_path: Path) -> None:
    write_files(repo, {"fixtures/legacy.py": HYDRATION, "fixtures/clean.py": PURE_ONLY, "fixtures/broken.py": "def (:\n"})
    policy = mini_policy(exclude=["fixtures"])
    found, scan = run_own(policy, repo, services(cache=tmp_path / "cache"), practices=False)
    notes = [v for v in found if v.rule == Rule.MIXED_PURITY_EXCLUDED]
    assert [v.subject for v in notes] == ["fixtures.legacy"]
    assert "fixtures/legacy.py is excluded" in notes[0].detail and "filesystem" in notes[0].hint
    assert notes[0] not in blocking(found)  # informational: the user excluded it on purpose, but it is no longer silent
    assert set(scan.excluded_modules) == {"fixtures.legacy", "fixtures.clean"}  # the file that does not parse is skipped
    warm = run_own(policy, repo, services(cache=tmp_path / "cache"), practices=False)[1]
    assert warm.parsed == 0 and set(warm.excluded_modules) == set(scan.excluded_modules)


def test_excluded_paths_are_not_parsed_when_the_rule_is_off(repo: Path) -> None:
    write_files(repo, {"fixtures/legacy.py": HYDRATION})
    scan = FsScanner().scan(mini_policy(exclude=["fixtures"], mixed_purity={"enabled": False}), repo)
    assert scan.excluded_modules == {}


def test_selftest_mutants_for_the_rule_are_killed_and_a_disabled_rule_is_reported_not_skipped_silently(repo: Path) -> None:
    policy = mini_policy(ring=RINGS)
    mutants, _ = plan(policy, FsScanner().scan(policy, repo))
    mixed = [m for m in mutants if m.rule == Rule.MIXED_PURITY]
    assert len(mixed) == 3  # a print in a function, a print at import time, a logging call
    report = selftest(policy, repo, services())
    assert [k for k in report.killed if "[mixed-purity]" in k] and not [s for s in report.survived if "[mixed-purity]" in s]
    off = mini_policy(ring=RINGS, mixed_purity={"enabled": False})
    off_mutants, off_skipped = plan(off, FsScanner().scan(off, repo))
    assert not [m for m in off_mutants if m.rule == Rule.MIXED_PURITY]
    assert "mixed-purity: disabled by policy" in off_skipped


def test_the_purity_command_ranks_by_blast_radius_and_marks_allowed_modules(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {
        "app/__init__.py": "", "app/domain.py": HYDRATION, "app/adapters.py": HYDRATION,
        "app/ports.py": "from . import domain\n", "app/main.py": "from . import domain, adapters\n",
    })
    policy = mini_policy(ring=RINGS, mixed_purity=allow({"module": "app.adapters", "reason": "ranked, but allowed"}))
    assert run_purity(policy, repo, services(), as_json=False) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "[domain] 1 mixed module(s)" and out[2].lstrip().startswith("app.domain:") and "imported by 2 modules" in out[2]
    assert out[3] == "[adapters] 1 mixed module(s)" and out[4].lstrip().startswith("app.adapters:") and " (allowed)" in out[4]
    assert "imported by 1 module;" in out[4]
    assert out[-1] == "2 mixed module(s)"
    assert run_purity(policy, repo, services(), as_json=True) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["module"], r["importers"], r["allowed"]) for r in rows] == [("app.domain", 2, False), ("app.adapters", 1, True)]
    write_files(repo, {"app/domain.py": PURE_ONLY, "app/adapters.py": EFFECT_ONLY})
    assert run_purity(policy, repo, services(), as_json=False) == 0
    assert capsys.readouterr().out.strip() == "no mixed modules"


def test_report_excluded_can_be_switched_off_on_its_own(repo: Path) -> None:
    write_files(repo, {"fixtures/legacy.py": HYDRATION})
    scan = FsScanner().scan(mini_policy(exclude=["fixtures"], mixed_purity={"report_excluded": False}), repo)
    assert scan.excluded_modules == {}
    assert len(run(repo, {"app/domain.py": HYDRATION}, mixed_purity={"report_excluded": False})) == 1  # the rule itself stays on


def test_a_partial_changed_scan_does_not_call_an_unparsed_allowed_module_stale(repo: Path) -> None:
    write_files(repo, {"app/__init__.py": "", "app/domain.py": HYDRATION, "app/ports.py": "VALUE = 1\n"})
    policy = mini_policy(
        ring=RINGS, mixed_purity=allow({"module": "app.domain", "reason": "kept"}, {"module": "app.gone", "reason": "was split"}),
    )
    scan = FsScanner().scan(policy, repo, only=frozenset({"app/ports.py"}))
    assert "app.domain" not in scan.modules
    found = [v for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index) if v.rule == Rule.MIXED_PURITY]
    assert [v.subject for v in found] == ["app.gone"]  # absence from the tree is still provable; "not mixed" is not


PURE_BODY = "    a = value + 1\n    b = a * 2\n    c = b - 3\n    return c\n"
FALLBACK = (
    "import logging\n\nlog = logging.getLogger(__name__)\n\n"
    "try:\n    import orjson\n\n    def dumps(x):\n        log.debug('encoding')\n        return orjson.dumps(x)\n"
    "except ImportError:\n    import json\n\n    def dumps(x):\n        return json.dumps(x)\n\n"
    f"def one(value):\n{PURE_BODY}\n\ndef two(value):\n{PURE_BODY}"
)


def test_a_function_defined_in_a_module_level_try_is_a_unit_not_import_time_code() -> None:
    """Review finding: only `tree.body` was searched for definitions, so logging inside an `except ImportError` fallback
    was reported as logging at import time and the function was not a unit."""
    purity = extract(FALLBACK, "m.py").purity
    assert purity.import_time == ()
    assert {unit.name for unit in purity.units} == {"dumps", "one", "two"}
    assert any(kind == "logging" for unit in purity.units if unit.name == "dumps" for _, kind in unit.kinds)


def test_two_pure_definitions_of_one_name_count_as_one_pure_unit() -> None:
    dumps = (
        "try:\n    import orjson\n\n    def dumps(value):\n        a = value + 1\n        b = a * 2\n        c = b - 3\n        return orjson.dumps(c)\n"
        "except ImportError:\n    import json\n\n    def dumps(value):\n        a = value + 1\n        b = a * 2\n        c = b - 3\n        return json.dumps(c)\n"
    )
    effect = "\n\ndef announce():\n    print('x')\n    return 1\n"
    one_logical = ModuleInfo("m", "m.py", False, extract(dumps + effect, "m.py"))
    two_logical = ModuleInfo("m", "m.py", False, extract(dumps + f"\n\ndef other(value):\n{PURE_BODY}" + effect, "m.py"))
    assert assess(one_logical, mini_policy()) is None  # one pure definition (written twice) is below min_pure_units
    report = assess(two_logical, mini_policy())
    assert report is not None and report.pure == ("dumps", "other")
