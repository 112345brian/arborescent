"""``unguarded-module-state`` (#232): a write to module-level mutable state that no module-level lock guards. Opt-in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.baseline import RecordOptions, load, record
from arborescent.config import load_policy
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.engine import blocking, run_own
from arborescent.facts import extract
from arborescent.selftest import plan, selftest

ERROR = "unguarded-module-state"
NOTE = "unguarded-module-state-note"
HEAD = "import threading\n\nREG = {}\nLOCK = threading.Lock()\n\n\n"


def writes(source: str) -> set[tuple[str, str]]:
    return {(w.name, w.function) for w in extract(source, "m.py").state_writes}


@pytest.mark.parametrize("body", [
    "REG[k] = 1", "REG[k] += 1", "del REG[k]", "REG.update(k)", "REG.pop(k, None)", "REG.clear()", "REG.setdefault(k, 1)",
])
def test_every_write_form_outside_a_lock_is_recorded(body: str) -> None:
    assert writes(f"{HEAD}def f(k):\n    {body}\n") == {("REG", "f")}


@pytest.mark.parametrize("statement", ["x = 1", "x += 1", "for x in range(3):\n        pass", "x, y = 1, 2", "del x"])
def test_a_global_rebinding_outside_a_lock_is_recorded(statement: str) -> None:
    assert ("x", "f") in writes(f"x = 0\n\n\ndef f():\n    global x\n    {statement}\n")


@pytest.mark.parametrize("lock", [
    "threading.Lock()", "threading.RLock()", "threading.Condition()", "multiprocessing.Lock()",
])
def test_a_write_inside_a_module_lock_is_guarded(lock: str) -> None:
    source = f"import multiprocessing\nimport threading\n\nREG = {{}}\nL = {lock}\n\n\ndef f():\n    with L:\n        REG['a'] = 1\n"
    assert writes(source) == set()


@pytest.mark.parametrize("source", [
    "from threading import Lock\nREG = []\nL: Lock = Lock()\n\n\ndef f():\n    with L:\n        REG.append(1)\n",
    "from threading import RLock as R\nREG = []\ntry:\n    L = R()\nexcept ImportError:\n    L = None\n\n\ndef f():\n    with L:\n        REG.append(1)\n",
    HEAD + "def f():\n    with LOCK: REG['a'] = 1\n",  # one line
    HEAD + "def f(p):\n    with open(p), LOCK:\n        if p:\n            for k in p:\n                REG[k] = 1\n",
    HEAD + "class A:\n    def f(self):\n        with LOCK:\n            REG['a'] = 1\n",
    HEAD + "x = 0\n\n\ndef f():\n    global x\n    with LOCK:\n        x = 1\n",
    HEAD + "def f():\n    REG = {}\n    REG['a'] = 1\n",  # a local, not the module's dict
    HEAD + "REG['a'] = 1\n\n\ndef f():\n    return REG.get('a')\n",  # import-time fill and a read
    "def f():\n    x = 1\n",
    "",
    "x = 0\n\n\ndef f():\n    global x\n    return x\n",  # a read-only global
])
def test_guarded_local_import_time_and_read_only_code_is_not_recorded(source: str) -> None:
    assert writes(source) == set()


@pytest.mark.parametrize("body", [
    "with open('p'):\n        REG['a'] = 1",
    "with threading.Lock():\n        REG['a'] = 1",  # a fresh lock guards nothing
    "with LOCK:\n        pass\n    REG['a'] = 1",  # after the block
    "if LOCK:\n        REG['a'] = 1",
    "LOCK.acquire()\n    REG['a'] = 1\n    LOCK.release()",  # documented blind spot: acquire/release is not read
])
def test_a_write_no_module_lock_encloses_is_recorded(body: str) -> None:
    assert writes(f"{HEAD}def f():\n    {body}\n") == {("REG", "f")}


def test_a_lock_around_the_definition_does_not_guard_the_call() -> None:
    source = HEAD + "def outer():\n    with LOCK:\n        def inner():\n            REG['a'] = 1\n    return inner\n"
    assert writes(source) == {("REG", "inner")}


def test_one_guarded_write_does_not_cover_another() -> None:
    source = HEAD + "def f():\n    with LOCK:\n        REG['a'] = 1\n    REG['b'] = 2\n\n\ndef g():\n    with LOCK:\n        REG.clear()\n"
    assert [(w.line, w.function) for w in extract(source).state_writes] == [(10, "f")]


def test_decorators_and_with_names_travel_with_the_write_for_the_rule_to_judge() -> None:
    source = (
        "from . import locks\nfrom .sync import guarded\n\nREG = {}\n\n\n@guarded\ndef f():\n    with locks.L:\n        REG['a'] = 1\n"
    )
    (write,) = extract(source).state_writes
    assert write.held == (".locks.L",)
    assert write.decorators == (".sync.guarded",)


def test_two_functions_of_one_name_keep_their_own_global_effect_uses() -> None:
    """The refactor that shares the global writes with this rule must not merge what `effect-global` reported before."""
    source = "x = 0\n\n\nclass A:\n    def reset(self):\n        global x\n        x = 1\n\n\nclass B:\n    def reset(self):\n        global x\n        x = 2\n"
    assert [(u.line, u.target) for u in extract(source).effect_uses if u.category == "global"] == [(7, "reset.x"), (13, "reset.x")]


def test_module_locks_are_recorded() -> None:
    assert extract(HEAD).module_locks == ("LOCK",)


# --- the rule ----------------------------------------------------------------------------------------------------------

UNGUARDED = "REG: dict[str, int] = {}\n\n\ndef add(k: str) -> None:\n    REG[k] = 1\n"


def check(repo: Path, files: dict[str, str], table: dict[str, Any] | None = None, only: frozenset[str] | None = None) -> list[Violation]:
    write_files(repo, {**MINI_FILES, **files})
    overrides = {} if table is None else {"unguarded_module_state": table}
    found, _ = run_own(mini_policy(**overrides), repo, services(), only, practices=False)
    return [v for v in found if v.rule in {ERROR, NOTE}]


def test_the_rule_is_off_without_its_table(repo: Path) -> None:
    assert check(repo, {"app/adapters.py": UNGUARDED}) == []


def test_an_empty_table_checks_every_ring(repo: Path) -> None:
    (found,) = check(repo, {"app/adapters.py": UNGUARDED}, {})
    assert (found.rule, found.subject, found.line) == (ERROR, "app.adapters", 5)
    assert found.detail == "[adapters] module-level state 'REG' is written in add() outside a lock"
    assert "allow" in found.render()


def test_rings_make_it_an_error_there_and_a_note_elsewhere(repo: Path) -> None:
    found = check(repo, {"app/adapters.py": UNGUARDED, "app/domain.py": UNGUARDED}, {"rings": ["adapters"]})
    assert sorted((v.rule, v.subject) for v in found) == [(ERROR, "app.adapters"), (NOTE, "app.domain")]
    assert [v.subject for v in blocking(found)] == ["app.adapters"]


def test_one_finding_per_state_and_function_with_a_key_that_survives_line_moves(repo: Path) -> None:
    source = "REG = {}\n\n\ndef add(k):\n    REG[k] = 1\n    REG.pop(k)\n"
    (first,) = check(repo, {"app/adapters.py": source}, {})
    (moved,) = check(repo, {"app/adapters.py": "\n\n" + source}, {})
    assert first.key == moved.key and first.line != moved.line


@pytest.mark.parametrize("imported", ["from .locks import L", "from app.locks import L", "from . import locks", "import app.locks"])
def test_a_lock_defined_in_another_scanned_module_guards(repo: Path, imported: str) -> None:
    held = "L" if imported.endswith(" L") else "app.locks.L" if imported == "import app.locks" else "locks.L"
    source = f"{imported}\n\nREG = {{}}\n\n\ndef f():\n    with {held}:\n        REG['a'] = 1\n"
    files = {"app/locks.py": "import threading\n\nL = threading.Lock()\n", "app/adapters.py": source}
    assert check(repo, files, {}) == []


def test_an_imported_name_that_is_not_a_lock_does_not_guard(repo: Path) -> None:
    source = "from .domain import VALUE\n\nREG = {}\n\n\ndef f():\n    with VALUE:\n        REG['a'] = 1\n"
    assert len(check(repo, {"app/adapters.py": source}, {})) == 1


def test_a_partial_scan_does_not_judge_a_lock_it_did_not_read(repo: Path) -> None:
    source = "from .locks import L\n\nREG = {}\n\n\ndef f():\n    with L:\n        REG['a'] = 1\n"
    files = {"app/locks.py": "import threading\n\nL = threading.Lock()\n", "app/adapters.py": source}
    stale = {"module": "app.domain", "name": "X", "reason": "r"}
    assert check(repo, files, {"allow": [stale]}, only=frozenset({"app/adapters.py"})) == []


def test_configured_locks_and_guard_decorators(repo: Path) -> None:
    source = (
        "from . import sync\nfrom .sync import guarded\n\nREG = {}\n\n\n@guarded\ndef a():\n    REG['a'] = 1\n\n\n"
        "@sync.synchronized(1)\ndef b():\n    REG['b'] = 1\n\n\ndef c():\n    with sync.factory('k'):\n        REG['c'] = 1\n"
    )
    files = {"app/adapters.py": source, "app/sync.py": ""}
    assert len(check(repo, files, {})) == 3
    table = {"guard_decorators": ["app.sync.guarded", "app.sync.synchronized"], "locks": ["app.sync.factory"]}
    assert check(repo, files, table) == []


def test_a_guard_decorator_defined_in_the_same_module(repo: Path) -> None:
    source = "REG = {}\n\n\ndef locked(f):\n    return f\n\n\n@locked\ndef a():\n    REG['a'] = 1\n"
    assert check(repo, {"app/adapters.py": source}, {"guard_decorators": ["app.adapters.locked"]}) == []


def test_allow_exempts_a_name_and_a_stale_entry_is_reported(repo: Path) -> None:
    allow = {"module": "app.adapters", "name": "REG", "reason": "written only by the single-threaded entrypoint"}
    assert check(repo, {"app/adapters.py": UNGUARDED}, {"allow": [allow]}) == []
    (unused,) = check(repo, {"app/adapters.py": "X = 1\n"}, {"allow": [allow]})
    assert (unused.rule, unused.subject) == (ERROR, "app.adapters") and "remove the entry" in unused.detail
    (missing,) = check(repo, {"app/adapters.py": "X = 1\n"}, {"allow": [{**allow, "module": "app.gone"}]})
    assert "matches no module" in missing.detail


@pytest.mark.parametrize("table", [
    "yes", {"unknown": 1}, {"rings": []}, {"rings": ["nowhere"]}, {"rings": "adapters"}, {"locks": [""]},
    {"guard_decorators": [1]}, {"allow": "REG"}, {"allow": [{"module": "a", "name": "REG"}]},
    {"allow": [{"module": "a", "name": "REG", "reason": "  "}]}, {"allow": [{"name": "REG", "reason": "r"}]},
    {"allow": [{"module": "a", "reason": "r"}]}, {"allow": [{"module": "a", "name": "REG", "reason": "r", "extra": 1}]},
])
def test_a_malformed_table_is_a_policy_error(table: Any) -> None:
    with pytest.raises(PolicyError):
        _ = mini_policy(unguarded_module_state=table)


def test_a_scoped_policy_reads_the_root_table_and_knows_every_scopes_rings(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "arborescent.toml": (
            '[unguarded_module_state]\nrings = ["web"]\n\n'
            '[scopes.one]\nsources = ["one"]\nsource_roots = ["."]\nrings = ["core"]\nclassify = {"one" = "core"}\n\n'
            '[scopes.two]\nsources = ["two"]\nsource_roots = ["."]\nrings = ["web"]\nclassify = {"two" = "web"}\n'
        ),
        "one/__init__.py": UNGUARDED,
        "two/__init__.py": UNGUARDED,
    })
    policy, root = load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")
    found, _ = run_own(policy, root, services(), practices=False)
    assert {(v.rule, v.subject) for v in found if v.rule in {ERROR, NOTE}} == {(NOTE, "one"), (ERROR, "two")}


def test_warm_cache_facts_and_findings_equal_cold(tmp_path: Path) -> None:
    source = HEAD + "from .sync import guarded\n\n\n@guarded\ndef a():\n    with open('p'):\n        REG['a'] = 1\n"
    write_files(tmp_path, {**MINI_FILES, "app/adapters.py": source})
    policy = mini_policy(unguarded_module_state={})
    scanner = FsScanner(tmp_path / "cache")
    cold = scanner.scan(policy, tmp_path)
    warm = scanner.scan(policy, tmp_path)
    assert warm.parsed == 0
    assert warm.modules["app.adapters"].facts == cold.modules["app.adapters"].facts == extract(source, "app/adapters.py")
    assert warm.modules["app.adapters"].facts.state_writes and warm.modules["app.adapters"].facts.module_locks == ("LOCK",)


def test_the_baseline_adopts_the_family_once(repo: Path) -> None:
    found = check(repo, {"app/adapters.py": UNGUARDED}, {})
    fs, path = LocalFileSystem(), repo / "baseline.json"
    _ = record(fs, path, [])
    _ = record(fs, path, found, RecordOptions(adopt=ERROR))
    assert load(fs, path) == {found[0].key}


def test_selftest_probes_the_rule_when_enabled_and_the_probe_is_caught(repo: Path) -> None:
    write_files(repo, MINI_FILES)
    policy = mini_policy(unguarded_module_state={"rings": ["adapters"]})
    mutations = plan(policy, FsScanner().scan(policy, repo))[0]
    assert [m.file for m in mutations if m.rule == Rule.UNGUARDED_MODULE_STATE] == ["app/adapters.py"]
    report = selftest(policy, repo, services())
    assert report.survived == []
    assert any(entry.startswith(f"[{ERROR}]") for entry in report.killed)


def test_selftest_has_no_probe_when_off_and_says_why_when_every_host_is_allowed(repo: Path) -> None:
    write_files(repo, MINI_FILES)
    off = mini_policy()
    assert Rule.UNGUARDED_MODULE_STATE not in {m.rule for m in plan(off, FsScanner().scan(off, repo))[0]}
    allowed = mini_policy(unguarded_module_state={"allow": [{"module": "app*", "name": "*", "reason": "r"}]})
    mutations, skipped = plan(allowed, FsScanner().scan(allowed, repo))
    assert Rule.UNGUARDED_MODULE_STATE not in {m.rule for m in mutations}
    assert any(entry.startswith(f"{ERROR}:") for entry in skipped)
