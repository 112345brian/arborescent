"""#215: function-level imports that are really eager CLI command modules (opt-in `[coupling.eager_commands]`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_baseline_ratchet import write_policy

from arborescent.check_runner import run_check
from arborescent.cli import main
from arborescent.command_imports import (
    COMMAND_BODY_HINT,
    command_bodies,
    eager_share,
    registered_targets,
)
from arborescent.coupling_policy import DEFAULT_COMMAND_DECORATORS, EagerCommands
from arborescent.domain import PolicyError, Rule
from arborescent.engine import Options, run_own

TYPER = (
    "import typer\n\napp = typer.Typer()\n\n\n"
    "@app.command()\ndef one() -> None:\n    import json\n    import re\n\n\n"
    "@app.command('second')\ndef two() -> None:\n    from app import domain\n\n\n"
    "def helper() -> None:\n    import csv\n"
)
ON = {"eager_commands": {}}


def bodies(source: str, decorators: tuple[str, ...] = DEFAULT_COMMAND_DECORATORS) -> dict[str, list[int]]:
    return {name: sorted(lines) for name, lines in command_bodies(source, decorators)}


def test_decorated_functions_are_commands_and_helpers_are_not() -> None:
    assert bodies(TYPER) == {"one": [8, 9], "two": [14]}


@pytest.mark.parametrize(
    "source",
    [
        "@cli.command\ndef c():\n    import a\n",
        "@command()\ndef c():\n    import a\n",
        "@app.callback()\ndef c():\n    import a\n",
        "@click.group()\ndef c():\n    import a\n",
        "def c():\n    import a\napp.command('x')(c)\n",
        "def c(args):\n    import a\nsub.add_parser('x').set_defaults(func=c)\n",
        "class K:\n    @app.command()\n    async def c(self):\n        import a\n",
        "def register(app):\n    @app.command()\n    def c():\n        import a\n",
        "@app.command()\ndef c():\n    def inner():\n        import a\n",
    ],
)
def test_each_registration_form_is_found(source: str) -> None:
    found = bodies(source)
    assert list(found) == ["c"] and len(found["c"]) == 1


def test_configured_decorators_replace_the_default_and_a_dotted_one_matches_whole() -> None:
    source = "@app.task\ndef a():\n    import x\n@other.task\ndef b():\n    import y\n@app.command\ndef c():\n    import z\n"
    assert list(bodies(source, ("task",))) == ["a", "b"]
    assert list(bodies(source, ("app.task",))) == ["a"]


@pytest.mark.parametrize("source", ["", "\n", "x = 1\n", "def f(:\n", "x\x00 = 1\n", "@app.command()\ndef c():\n    pass\n"])
def test_empty_broken_and_importless_sources_have_no_command_imports(source: str) -> None:
    assert command_bodies(source, DEFAULT_COMMAND_DECORATORS) == ()


def test_a_byte_order_mark_does_not_hide_commands() -> None:
    assert list(bodies("\ufeff" + TYPER)) == ["one", "two"]


def test_share_needs_enough_imports_enough_share_and_two_commands() -> None:
    lazy = [8, 9, 14, 18]
    found = eager_share(TYPER, lazy, EagerCommands())
    assert found is not None
    assert (found.inside, found.total, found.commands, found.lines) == (3, 4, ("one", "two"), frozenset({8, 9, 14}))
    assert eager_share(TYPER, lazy, EagerCommands(min_imports=4)) is None
    assert eager_share(TYPER, lazy, EagerCommands(min_share=0.8)) is None
    assert eager_share(TYPER, lazy, EagerCommands(min_commands=3)) is None
    assert eager_share(TYPER, [], EagerCommands(min_imports=1, min_commands=1)) is None


def test_two_names_on_one_line_count_twice() -> None:
    source = "@app.command\ndef a():\n    import x, y\n@app.command\ndef b():\n    import z\n"
    found = eager_share(source, [3, 3, 6], EagerCommands())
    assert found is not None and found.inside == 3


def test_the_check_is_off_by_default(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": TYPER})
    report = run_check(mini_policy(), repo, services(), Options(tools=False))
    assert not [v for v in report.notes if v.rule == Rule.ENTRYPOINT_EAGER_COMMANDS]
    assert not [v for v in report.violations if v.hint == COMMAND_BODY_HINT]


def test_enabled_it_adds_one_note_and_keeps_every_lazy_import_key(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": TYPER})
    plain = run_check(mini_policy(), repo, services(), Options(tools=False))
    flagged = run_check(mini_policy(coupling=ON), repo, services(), Options(tools=False))
    assert [v.key for v in flagged.violations] == [v.key for v in plain.violations]  # the ratchet is untouched
    (note,) = [v for v in flagged.notes if v.rule == Rule.ENTRYPOINT_EAGER_COMMANDS]
    assert (note.subject, note.line) == ("app.adapters", 8)
    assert "3 of 4 function-level imports are inside 2 command bodies (one, two)" in note.detail
    assert "+1 project module" in note.detail  # app.domain
    assert [v.detail for v in flagged.violations if v.hint == COMMAND_BODY_HINT] == ["json#1", "re#1", "app#1"]


def test_the_hoisting_cost_counts_parent_packages(repo: Path) -> None:
    source = TYPER.replace("from app import domain", "from app.extra import leaf")
    write_files(repo, {"app/adapters.py": source, "app/extra/__init__.py": "", "app/extra/leaf.py": ""})
    found, _ = run_own(mini_policy(coupling=ON), repo, services(), practices=False)
    (note,) = [v for v in found if v.rule == Rule.ENTRYPOINT_EAGER_COMMANDS]
    assert "+2 project module" in note.detail  # app.extra.leaf and the package app.extra it runs


def test_deferred_comments_do_not_hide_the_pattern(repo: Path) -> None:
    source = TYPER.replace("import json", "import json  # deferred: startup").replace("import re", "import re  # deferred: startup")
    write_files(repo, {"app/adapters.py": source})
    found, _ = run_own(mini_policy(coupling=ON), repo, services(), practices=False)
    assert [v.subject for v in found if v.rule == Rule.ENTRYPOINT_EAGER_COMMANDS] == ["app.adapters"]


def test_a_partial_scan_does_not_state_a_closure_cost(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": TYPER})
    found, _ = run_own(mini_policy(coupling=ON), repo, services(), frozenset({"app/adapters.py"}), practices=False)
    (note,) = [v for v in found if v.rule == Rule.ENTRYPOINT_EAGER_COMMANDS]
    assert "project module" not in note.detail


def test_new_command_imports_print_as_one_line_and_still_fail(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    with config.open("a", encoding="utf-8") as handle:
        handle.write("[coupling.eager_commands]\n")
    write_files(repo, {"app/adapters.py": TYPER})
    assert main(["--config", str(config), "check", "--no-tools"], services()) == 1
    out = capsys.readouterr().out
    fails = [line for line in out.splitlines() if line.startswith("FAIL") and "[lazy-import]" in line]
    assert len(fails) == 2  # one for the three command-body imports, one for the helper's
    assert any("app.adapters: [lazy-import] 3 function-level imports in command bodies: json#1, re#1, app#1" in line for line in fails)
    assert "[entrypoint-eager-commands]" in out and "one module per command" in out


@pytest.mark.parametrize(
    "table",
    [
        [],
        {"nope": 1},
        {"decorators": []},
        {"decorators": "command"},
        {"decorators": [""]},
        {"min_imports": 0},
        {"min_imports": True},
        {"min_commands": 0},
        {"min_share": 0},
        {"min_share": 1.5},
        {"min_share": "half"},
    ],
)
def test_a_malformed_table_is_refused(table: object) -> None:
    with pytest.raises(PolicyError):
        mini_policy(coupling={"eager_commands": table})


def test_the_table_is_read() -> None:
    assert mini_policy().coupling.eager_commands is None
    assert mini_policy(coupling=ON).coupling.eager_commands == EagerCommands()
    policy = mini_policy(coupling={"eager_commands": {"decorators": ["task"], "min_imports": 5, "min_share": 1, "min_commands": 3}})
    assert policy.coupling.eager_commands == EagerCommands(("task",), 5, 1.0, 3)


def test_many_commands_stay_fast() -> None:
    source = "".join(f"@app.command()\ndef c{n}():\n    import m{n}\n" for n in range(2000))
    found = eager_share(source, [3 * n + 3 for n in range(2000)], EagerCommands())
    assert found is not None and found.inside == 2000


HANDLERS = "def one(args) -> None:\n    import json\n    import re\n\n\ndef two(args) -> None:\n    from app import domain\n"


@pytest.mark.parametrize(
    "registrar",
    [
        "from app import adapters\nsub.add_parser('a').set_defaults(func=adapters.one)\np.set_defaults(func=adapters.two)\n",
        "from app.adapters import one, two as other\np.set_defaults(func=one)\nq.set_defaults(func=other)\n",
        "from .adapters import one, two\np.set_defaults(func=one)\nq.set_defaults(func=two)\n",
        "from . import adapters\np.set_defaults(func=adapters.one)\nq.set_defaults(func=adapters.two)\n",
        "import app.adapters\np.set_defaults(func=app.adapters.one)\nq.set_defaults(func=app.adapters.two)\n",
        "def build(p, q):\n    from app.adapters import one, two\n    p.set_defaults(func=one)\n    q.set_defaults(func=two)\n",
        "from app import adapters\napp.command('a')(adapters.one)\napp.command('b')(adapters.two)\n",
    ],
)
def test_handlers_registered_from_another_module_are_commands(repo: Path, registrar: str) -> None:
    assert registered_targets(registrar, "app.main", False, DEFAULT_COMMAND_DECORATORS) == {"app.adapters.one", "app.adapters.two"}
    write_files(repo, {"app/adapters.py": HANDLERS, "app/main.py": registrar})
    found, _ = run_own(mini_policy(coupling=ON), repo, services(), practices=False)
    assert [v.subject for v in found if v.rule == Rule.ENTRYPOINT_EAGER_COMMANDS] == ["app.adapters"]


@pytest.mark.parametrize(
    "registrar",
    ["p.set_defaults(func=one)\n", "p.set_defaults(func=args.one, verbose=True)\n", "def f(:\n", "", "x\x00 = 'set_defaults'\n"],
)
def test_names_that_are_not_imported_register_nothing_elsewhere(registrar: str) -> None:
    assert registered_targets(registrar, "app.main", False, DEFAULT_COMMAND_DECORATORS) == frozenset()


def test_an_external_registration_names_only_a_module_level_function() -> None:
    source = "class K:\n    def one(self):\n        import a\ndef outer():\n    def two():\n        import b\n"
    assert command_bodies(source, DEFAULT_COMMAND_DECORATORS, frozenset({"one", "two"})) == ()
