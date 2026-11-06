"""Effect categories `console` (print, input, sys.stdout/stderr.write) and `global` (a function that rebinds a module global)."""

from __future__ import annotations

from arborescent.facts import extract


def _uses(source: str, category: str) -> set[tuple[int, str]]:
    return {(use.line, use.target) for use in extract(source).effect_uses if use.category == category}


def test_console_covers_print_input_and_the_standard_streams() -> None:
    source = "import sys\nprint('x')\nname = input()\nsys.stdout.write('a')\nsys.stderr.write('b')\n"
    assert _uses(source, "console") == {(2, "print"), (3, "input"), (4, "sys.stdout.write"), (5, "sys.stderr.write")}


def test_console_resolves_import_aliases() -> None:
    assert _uses("import sys as system\nsystem.stdout.write('a')\n", "console") == {(2, "sys.stdout.write")}
    assert _uses("from sys import stderr\nstderr.write('a')\n", "console") == {(2, "sys.stderr.write")}


def test_console_ignores_look_alikes() -> None:
    source = "import logging\nlogger = logging.getLogger()\nlogger.info('x')\nself.print('x')\nconsole.print('x')\nbuffer.write('x')\n"
    assert _uses(source, "console") == set()


def test_a_rebound_print_or_input_is_not_the_builtin() -> None:
    assert _uses("def run(input):\n    return input.strip()\n", "console") == set()
    assert _uses("def print(*args):\n    pass\nprint('x')\n", "console") == set()
    assert _uses("input = {}\nvalue = input\n", "console") == set()


def test_console_in_a_bare_parameter_default_is_an_injection_seam() -> None:
    facts = extract("def run(out=print):\n    out('x')\n")
    (use,) = [use for use in facts.effect_uses if use.category == "console"]
    assert use.seam != 0
    (call,) = [use for use in extract("def run():\n    print('x')\n").effect_uses if use.category == "console"]
    assert call.seam == 0


def test_global_counts_a_function_that_rebinds_a_module_global() -> None:
    source = "_slot = None\n\ndef bind(value):\n    global _slot\n    _slot = value\n"
    assert _uses(source, "global") == {(5, "global _slot")}


def test_global_counts_augmented_assignment_delete_def_and_import() -> None:
    source = (
        "count = 0\ndef a():\n    global count\n    count += 1\n"
        "def b():\n    global count\n    del count\n"
        "def c():\n    global helper\n    def helper(): ...\n"
        "def d():\n    global os\n    import os\n"
    )
    assert {target for _, target in _uses(source, "global")} == {"global count", "global helper", "global os"}


def test_global_declaration_that_only_reads_is_not_a_use() -> None:
    assert _uses("x = 1\ndef read():\n    global x\n    return x\n", "global") == set()


def test_global_assignment_of_an_undeclared_name_is_a_local() -> None:
    assert _uses("x = 1\ndef f():\n    x = 2\n    return x\n", "global") == set()


def test_global_declared_in_one_function_does_not_cover_a_nested_function() -> None:
    source = "x = 1\ndef outer():\n    global x\n    def inner():\n        x = 5\n        return x\n    return inner\n"
    assert _uses(source, "global") == set()


def test_global_in_a_nested_function_is_attributed_to_that_function() -> None:
    source = "x = 1\ndef outer():\n    def inner():\n        global x\n        x = 5\n    return inner\n"
    assert _uses(source, "global") == {(5, "global x")}


def test_global_reports_each_name_once_at_its_first_assignment() -> None:
    source = "x = 0\ndef f():\n    global x\n    x = 1\n    x = 2\n"
    assert _uses(source, "global") == {(4, "global x")}


def test_global_works_in_async_functions() -> None:
    assert _uses("x = 0\nasync def f():\n    global x\n    x = 1\n", "global") == {(4, "global x")}
