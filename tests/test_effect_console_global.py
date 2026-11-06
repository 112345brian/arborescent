"""Rules `effect-console` and `effect-global`, end to end through `check_modules` (#103)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.rules import check_modules

CONSOLE = "import sys\ndef show(text):\n    print(text)\n    sys.stdout.write(text)\n    return input()\n"
GLOBAL = "_slot = None\ndef bind(value):\n    global _slot\n    _slot = value\n"


def run(root: Path, **overrides: Any) -> list[Any]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def effects(found: list[Any]) -> set[tuple[str, str]]:
    # The direct rules only: `effect-via-import` has its own tests (test_effect_via_import.py).
    return {(finding.rule, finding.subject) for finding in found if finding.rule.startswith("effect-") and finding.rule != "effect-via-import"}


def test_a_ring_that_forbids_console_flags_print_input_and_stream_writes(repo: Path) -> None:
    write_files(repo, {"app/domain.py": CONSOLE})
    rings = ring_with(domain={"external": ["sys"], "forbid": ["console"]})

    found = [finding for finding in run(repo, ring=rings) if finding.rule == "effect-console"]

    assert {finding.line for finding in found} == {3, 4, 5}
    assert all("console" in finding.detail for finding in found)


def test_the_innermost_ring_forbids_console_and_global_by_default(repo: Path) -> None:
    write_files(repo, {"app/domain.py": CONSOLE + GLOBAL})

    assert effects(run(repo, ring=ring_with(domain={"external": ["sys"]}))) == {
        ("effect-console", "app.domain"),
        ("effect-global", "app.domain"),
    }


def test_the_outermost_ring_may_print_and_bind_globals(repo: Path) -> None:
    write_files(repo, {"app/main.py": CONSOLE + GLOBAL})
    rings = ring_with(composition={"may_import": ["*"], "external": ["sys"]})

    assert effects(run(repo, ring=rings)) == set()


def test_an_explicit_empty_forbid_allows_both(repo: Path) -> None:
    write_files(repo, {"app/domain.py": CONSOLE + GLOBAL})
    rings = ring_with(domain={"external": ["sys"], "forbid": []})

    assert effects(run(repo, ring=rings)) == set()


def test_forbidding_one_of_the_two_leaves_the_other_alone(repo: Path) -> None:
    write_files(repo, {"app/domain.py": CONSOLE + GLOBAL})

    console_only = ring_with(domain={"external": ["sys"], "forbid": ["console"]})
    global_only = ring_with(domain={"external": ["sys"], "forbid": ["global"]})

    assert effects(run(repo, ring=console_only)) == {("effect-console", "app.domain")}
    assert effects(run(repo, ring=global_only)) == {("effect-global", "app.domain")}


def test_a_logging_call_is_not_console_output(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import logging\nlog = logging.getLogger(__name__)\ndef f():\n    log.info('x')\n"})

    assert effects(run(repo, ring=ring_with(domain={"external": ["logging"]}))) == set()


def test_console_as_an_injected_default_is_not_flagged(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "def report(text, out=print):\n    out(text)\n"})

    assert effects(run(repo, ring=ring_with(domain={}))) == set()


def test_effect_defaults_count_makes_the_injected_console_default_a_use(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "def report(text, out=print):\n    out(text)\n"})

    assert effects(run(repo, ring=ring_with(domain={}), effect_defaults="count")) == {("effect-console", "app.domain")}
